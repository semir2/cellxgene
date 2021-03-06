"""
This program converts an [AnnData H5AD](https://anndata.readthedocs.io/en/stable/)
into a cellxgene TileDB structure, aka a 'CXG'.

The organization of the TileDB structure is:

    the.cxg                 TileDB Group
    |-- obs                 TileDB array containing cell (row) attributes, one attribute per
    |                       dataframe columm, shape (n_obs,)
    |-- var                 TileDB array containing gene (column) attributes, with one attribute per
    |                       dataframe column, shape (n_var,)
    |-- X                   Main count matrix as a 2D TileDB array, single unnanmed numeric attribute
    |-- emb                 TileDB group, storing optional embeddings (group may be empty)
    |   |-- <name1>         TileDB Array, single anon attribute, ND numeric array, shape (n_obs, N)
    |-- cxg_group_metadata  Empty array used only to stash metadata about the overall object.
    ...

All arrays are defined to have a uint32 domain, zero based.  All X counds and embedding
coordinates are coerced to float32, which is ample precision for visualization purposes.
Dataframe (metadata) types are generally preserved, or where that is not possible,
converted to somemthing with equal representative value in the cellxgene application
(eg, categorical types are converted to string, bools to uint8, etc).

The following objects are also decorated with auxilliary metadata using TileDB
array metadata:

* cxg_group_metadata: minimally, will contain a 'cxg_version' field, which
  is a semver string identifing the version number of the CXG layout.
  It may also contain 'cxg_parameters', a JSON-encoded parameter list
  describing CXG-wide dataset parameters.

* obs, var: both contain an optional 'cxg_schema' field that is a json string,
  containing per-column (attribute) schema hinting.  This is used where the TileDB
  native typing information is insufficient to reconstruct useful information
  such as categorical typing from Pandas DataFrames, and to communicate which column
  is the preferred human-readable index for obs & var.

This file also embodies a number of empirically derived tiledb schema parameters,
including the global data layout, spatial tile size, and the like. The CXG is
self-describing in these areas, and the actual values (eg, tile size) are empirically
derived from benchmarking. They may change in the future.

---

TODO/ISSUES:
* add sub-command structure to argparse, for future sub-commands
* Possible future work: accept Loom files

"""
import re
import anndata
import tiledb
import argparse
import numpy as np
from os.path import splitext, basename
import json


# the CXG container version number.  Must be a semver string.
CXG_VERSION = "0.1"


def log(level, *args):
    global log_level
    if log_level and level <= log_level:
        print(*args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("h5ad", nargs="?", help="H5AD file name")
    parser.add_argument(
        "--backed", action="store_true", help="loaded in file backed mode. Will be slower, but use less memory."
    )
    parser.add_argument(
        "--obs-names", help="Name of annotation to use for observations. If not specified, will use the obs index."
    )
    parser.add_argument(
        "--var-names", help="Name of annotation to use for variables. If not specified, will use the var index."
    )
    parser.add_argument("--verbose", "-v", action="count", default=0, help="verbose output")
    parser.add_argument("--title", help="Human readable dataset title.  If omitted, will use filename")
    parser.add_argument(
        "--about",
        metavar="<URL>",
        help="URL providing more information about the dataset (hint: must be a fully specified absolute URL).",
    )
    parser.add_argument("--out", "--output", "-o", help="output CXG file name")
    args = parser.parse_args()

    global log_level
    log_level = args.verbose

    adata = anndata.read_h5ad(args.h5ad, backed="r" if args.backed else None)
    log(1, f"{basename(args.h5ad)} loaded...")

    basefname = splitext(basename(args.h5ad))[0]
    out = args.out if args.out is not None else basefname
    container = out if splitext(out)[1] == ".cxg" else out + ".cxg"
    title = args.title if args.title is not None else basefname

    write_cxg(adata, container, title, var_names=args.var_names, obs_names=args.obs_names, about=args.about)

    log(1, "done")


def write_cxg(adata, container, title, var_names=None, obs_names=None, about=None):
    if not adata.var.index.is_unique:
        raise ValueError("Variable index is not unique - unable to convert.")
    if not adata.obs.index.is_unique:
        raise ValueError("Observation index is not unique - unable to convert.")

    """
    TileDB bug TileDB-Inc/TileDB#1575 requires that we sanitize all column names
    prior to saving.  This can be reverted when the bug is fixed.
    """
    log(0, "Warning: sanitizing all dataframe column names.")
    clean_all_column_names(adata)

    ctx = tiledb.Ctx(
        {
            "sm.num_reader_threads": 32,
            "sm.num_writer_threads": 32,
            "sm.consolidation.buffer_size": 1 * 1024 * 1024 * 1024,
        }
    )

    tiledb.group_create(container, ctx=ctx)
    log(1, f"\t...group created, with name {container}")

    # dataset metadata
    save_metadata(container, {"title": title, "about": about})
    log(1, "\t...dataset metadata saved")

    # var/gene dataframe
    save_dataframe(container, "var", adata.var, var_names, ctx=ctx)
    log(1, "\t...var dataframe created")

    # obs/cell dataframe
    save_dataframe(container, "obs", adata.obs, obs_names, ctx=ctx)
    log(1, "\t...obs dataframe created")

    # embeddings
    e_container = f"{container}/emb"
    tiledb.group_create(e_container, ctx=ctx)
    save_embeddings(e_container, adata, ctx)
    log(1, "\t...embeddings created")

    # X matrix
    save_X(container, adata, ctx)
    log(1, "\t...X created")


"""
TODO: the code used to handle type inferencing should not be duplicated between
this tool and the server/common/utils code. When this tool is merged into
the cellxgene CLI, consolidate.
"""


def dtype_to_schema(dtype):
    if dtype == np.float32:
        return (np.float32, {})
    elif dtype == np.int32:
        return (np.int32, {})
    elif dtype == np.bool_:
        return (np.uint8, {"type": "boolean"})
    elif dtype == np.str:
        return (np.unicode, {"type": "string"})
    elif dtype == "category":
        typ, hint = cxg_type(dtype.categories)
        return (typ, {"type": "categorical", "categories": dtype.categories.tolist()})
    else:
        raise TypeError(f"Annotations of type {dtype} are unsupported.")


def _can_cast_to_float32(array):
    if array.dtype.kind == "f":
        # force downcast for all floats
        return True
    return False


def _can_cast_to_int32(array):
    if array.dtype.kind in ["i", "u"]:
        if np.can_cast(array.dtype, np.int32):
            return True
        ii32 = np.iinfo(np.int32)
        if array.min() >= ii32.min and array.max() <= ii32.max:
            return True
    return False


def cxg_type(array):
    try:
        return dtype_to_schema(array.dtype)
    except TypeError:
        dtype = array.dtype
        data_kind = dtype.kind
        if _can_cast_to_float32(array):
            return (np.float32, {})
        elif _can_cast_to_int32(array):
            return (np.int32, {})
        elif data_kind == "O" and dtype == "object":
            return (np.unicode, {"type": "string"})
        else:
            raise TypeError(f"Annotations of type {dtype} are unsupported.")


def cxg_dtype(array):
    return cxg_type(array)[0]


def create_dataframe(name, df, ctx):
    """
    Current access patterns are oriented toward reading very large slices of
    the dataframe, one attribute at a time.  Attribute data also tends to be
    (often) repetitive (bools, categories, strings).
    Given this, we use:
    * a large tile size (1000)
    * very aggressive compression levels
    """
    filter = tiledb.FilterList(
        [
            # attempt aggressive compression as many of these dataframes are very repetitive
            # strings, bools and other non-float data.
            tiledb.ZstdFilter(level=22),
        ]
    )
    attrs = [tiledb.Attr(name=col, dtype=cxg_dtype(df[col]), filters=filter) for col in df]
    domain = tiledb.Domain(tiledb.Dim(domain=(0, df.shape[0] - 1), tile=min(df.shape[0], 1000), dtype=np.uint32))
    schema = tiledb.ArraySchema(
        domain=domain, sparse=False, attrs=attrs, cell_order="row-major", tile_order="row-major"
    )
    tiledb.DenseArray.create(name, schema)


def create_unique_column_name(df_cols, col_name_prefix):
    """
    given the columns of a dataframe, and a name prefix, return a column name which
    does not exist in the dataframe, AND which is prefixed by `prefix`

    The approach is to append a numeric suffix, starting at zero and increasing by
    one, until an unused name is found (eg, prefix_0, prefix_1, ...).
    """
    suffix = 0
    while f"{col_name_prefix}{suffix}" in df_cols:
        suffix += 1
    return f"{col_name_prefix}{suffix}"


def alias_index_col(df, df_name, index_col_name):
    """
    We rely in the existance of a unique, human-readable index for
    any dataframe (eg, var is typically gene name, obs the cell name).
    The user can specify these via the --obs-names and --var-names config.
    If they are not specified, use the existing index to create them, giving
    the resulting column a unique name (eg, "name").

    In both cases, enforce that the result is unique, and communicate the
    index column name via the 'index' field in the schema hints.
    """
    if index_col_name is None:
        if not df.index.is_unique:
            raise KeyError(
                f"Values in {df_name}.index must be unique. "
                "Please prepare data to contain unique index values, or specify an "
                "alternative with --{ax_name}-name."
            )
        index_col_name = create_unique_column_name(df.columns, "name_")
        # turn the index into a normal column
        df.rename_axis(index_col_name, inplace=True)
        df.reset_index(inplace=True)

    elif index_col_name in df.columns:
        # User has specified alternative column for unique names, and it exists
        if not df[index_col_name].is_unique:
            raise KeyError(
                f"Values in {df_name}.{index_col_name} must be unique. Please prepare data to contain unique values."
            )

    else:
        raise KeyError(f"Annotation {index_col_name}, specified in --{df_name}-name, does not exist.")

    return (df, index_col_name)


def save_dataframe(container, name, df, index_col_name, ctx):
    A_name = f"{container}/{name}"
    (df, index_col_name) = alias_index_col(df, name, index_col_name)
    create_dataframe(A_name, df, ctx=ctx)
    with tiledb.DenseArray(A_name, mode="w", ctx=ctx) as A:
        value = {}
        schema_hints = {}
        for k, v in df.items():
            dtype, hints = cxg_type(v)
            value[k] = v.to_numpy(dtype=dtype)
            if hints:
                schema_hints.update({k: hints})

        schema_hints.update({"index": index_col_name})
        A[:] = value
        A.meta["cxg_schema"] = json.dumps(schema_hints)

    tiledb.consolidate(A_name, ctx=ctx)


def create_emb(e_name, emb):
    """
    Embeddings are typically accessed with very large slices (or all of the embedding),
    and do not benefit from overly aggressive compression due to their format.  Given
    this, we use:
    * large tile size (1000)
    * default compression level
    """
    filters = tiledb.FilterList([tiledb.ZstdFilter()])
    attrs = [tiledb.Attr(dtype=emb.dtype, filters=filters)]
    dims = []
    for d in range(emb.ndim):
        shape = emb.shape
        dims.append(tiledb.Dim("", domain=(0, shape[d] - 1), tile=min(shape[d], 1000), dtype=np.uint32))
    domain = tiledb.Domain(*dims)
    schema = tiledb.ArraySchema(
        domain=domain, sparse=False, attrs=attrs, capacity=1_000_000, cell_order="row-major", tile_order="row-major"
    )
    tiledb.DenseArray.create(e_name, schema)


def is_valid_embedding(adata, name, arr):
    """ return True if this layout data is a valid array for front-end presentation:
        * ndarray, with shape (n_obs, >= 2), dtype float/int/uint
        * contains only finite values
        * follows ScanPy embedding naming conventions
    """
    is_valid = type(name) == str and name.startswith("X_") and len(name) > 2
    is_valid = is_valid and type(arr) == np.ndarray and arr.dtype.kind in "fiu"
    is_valid = is_valid and arr.shape[0] == adata.n_obs and arr.shape[1] >= 2
    is_valid = is_valid and np.all(np.isfinite(arr))
    return is_valid


def save_embeddings(container, adata, ctx):
    for (name, value) in adata.obsm.items():
        if is_valid_embedding(adata, name, value):
            e_name = f"{container}/{name[2:]}"
            create_emb(e_name, value)
            with tiledb.DenseArray(e_name, mode="w", ctx=ctx) as A:
                A[:] = value
            tiledb.consolidate(e_name, ctx=ctx)
            log(1, f"\t\t...{name} embedding created")


def create_X(X_name, shape):
    """
    Dense, always.  Future task: explore if sparse encoding is worth the trouble
    below a sparsity threshold.

    The X matrix is access in both row and column oriented patterns, depending on the
    particular operation.  Because of the data type, default compression works best.
    The tile size (50, 100) and global layout (row/col) was choosen empirically, by benchmarking
    the current cellxgene backend.
    """
    filters = tiledb.FilterList([tiledb.ZstdFilter()])
    attrs = [tiledb.Attr(dtype=np.float32, filters=filters)]
    domain = tiledb.Domain(
        tiledb.Dim(name="obs", domain=(0, shape[0] - 1), tile=min(shape[0], 50), dtype=np.uint32),
        tiledb.Dim(name="var", domain=(0, shape[1] - 1), tile=min(shape[1], 100), dtype=np.uint32),
    )
    schema = tiledb.ArraySchema(
        domain=domain, sparse=False, attrs=attrs, cell_order="row-major", tile_order="col-major"
    )
    tiledb.DenseArray.create(X_name, schema)


def save_X(container, adata, ctx):
    # Save X count matrix
    X_name = f"{container}/X"
    shape = adata.X.shape
    create_X(X_name, shape)

    stride = min(int(np.power(10, np.around(np.log10(1e9 / shape[1])))), 10_000)
    with tiledb.DenseArray(X_name, mode="w", ctx=ctx) as X:
        for row in range(0, shape[0], stride):
            lim = min(row + stride, shape[0])
            a = adata.X[row:lim, :]
            if type(a) is not np.ndarray:
                a = a.toarray()
            X[row:lim, :] = a
            log(2, "\t...rows", row, "to", lim)
        tiledb.consolidate(X_name, ctx=ctx)

    tiledb.consolidate(X_name, ctx=ctx)


def save_metadata(container, metadata):
    """
    Save all dataset-wide metadata.   This includes:
    * CXG version
    * dataset metadata, such as title and about link.

    Longer term, tiledb will have support for metadata on groups. Until
    such feature exists, create an empty array and annotate that array.

    https://github.com/TileDB-Inc/TileDB-Py/issues/254
    """
    a_name = f"{container}/cxg_group_metadata"
    with tiledb.from_numpy(a_name, np.zeros((1,))) as A:
        pass
    with tiledb.DenseArray(a_name, mode="w") as A:
        A.meta["cxg_version"] = CXG_VERSION
        A.meta["cxg_properties"] = json.dumps(metadata)


def sanitize_keys(keys):
    """
    We need names to be safe to use as attribute names in tiledb.  See:
        TileDB-Inc/TileDB#1575
        TileDB-Inc/TileDB-Py#294
    This can be entirely removed once they add proper escaping.

    Args: list of keys
    Returns: dict of {old_key: new_key, ...}

    Returned new keys will be both safe and unique.

    Masking out [~/.] and anything outside the ASCII range.
    """
    p = re.compile(r"[^ -\.0-\[\]-\}]")
    clean_keys = {k: p.sub("_", k) for k in keys}

    used_keys = set()
    clean_unique_keys = {}
    for k, v in clean_keys.items():
        if v not in used_keys:
            used_keys.add(v)
            clean_unique_keys[k] = v
            continue

        # else, needs deduping.
        counter = 1
        while True:
            candidate_name = v + "-" + str(counter)
            if candidate_name not in used_keys:
                used_keys.add(candidate_name)
                clean_unique_keys[k] = candidate_name
                break
            counter += 1

    for k, v, in clean_unique_keys.items():
        if k != v:
            log(1, f"Renaming {k} to {v}")
    return clean_unique_keys


def sanitize_df(df):
    df.rename(columns=sanitize_keys(df.keys().tolist()), inplace=True)


def sanitize_mapping(mapping):
    clean_keys = sanitize_keys([k for k in mapping.keys()])
    for old_key, new_key in clean_keys.items():
        if old_key != new_key:
            mapping[new_key] = mapping[old_key]
            del mapping[old_key]


def clean_all_column_names(adata):
    sanitize_df(adata.obs)
    sanitize_df(adata.var)
    sanitize_mapping(adata.obsm)


if __name__ == "__main__":
    main()
