import tables
import os
import glob

# from pprint import pp
import time

# from astropy.coordinates import Angle
import numpy as np
import pandas as pd
import pymongo
import random
import argparse
import traceback
import datetime
import pytz
from numba import jit

# from concurrent.futures import ThreadPoolExecutor
# from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp
from tqdm import tqdm

from utils import (
    deg2dms,
    deg2hms,
    load_config,
)


""" load config and secrets """
config = load_config(config_file="config.yaml")["kowalski"]


def utc_now():
    return datetime.datetime.now(pytz.utc)


def connect_to_db():
    """Connect to the mongodb database

    :return:
    """
    try:
        if config["database"]["replica_set"] is None:
            _client = pymongo.MongoClient(
                host=config["database"]["host"], port=config["database"]["port"]
            )
        else:
            _client = pymongo.MongoClient(
                host=config["database"]["host"],
                port=config["database"]["port"],
                replicaset=config["database"]["replica_set"],
            )
        # grab main database:
        _db = _client[config["database"]["db"]]
    except Exception:
        raise ConnectionRefusedError
    try:
        # authenticate
        _db.authenticate(config["database"]["username"], config["database"]["password"])
    except Exception:
        raise ConnectionRefusedError

    return _client, _db


def insert_db_entry(_db, _collection=None, _db_entry=None):
    """
        Insert a document _doc to collection _collection in DB.
        It is monitored for timeout in case DB connection hangs for some reason
    :param _collection:
    :param _db_entry:
    :return:
    """
    assert _collection is not None, "Must specify collection"
    assert _db_entry is not None, "Must specify document"
    try:
        _db[_collection].insert_one(_db_entry)
    except Exception as _e:
        print(
            "Error inserting {:s} into {:s}".format(str(_db_entry["_id"]), _collection)
        )
        traceback.print_exc()
        print(_e)


def insert_multiple_db_entries(_db, _collection=None, _db_entries=None, _verbose=False):
    """
        Insert a document _doc to collection _collection in DB.
        It is monitored for timeout in case DB connection hangs for some reason
    :param _db:
    :param _collection:
    :param _db_entries:
    :param _verbose:
    :return:
    """
    assert _collection is not None, "Must specify collection"
    assert _db_entries is not None, "Must specify documents"
    try:
        _db[_collection].insert_many(_db_entries, ordered=False)
    except pymongo.errors.BulkWriteError as bwe:
        if _verbose:
            print(bwe.details)
    except Exception as _e:
        if _verbose:
            traceback.print_exc()
            print(_e)


@jit(forceobj=True)
def ccd_quad_2_rc(ccd: int, quad: int) -> int:
    # assert ccd in range(1, 17)
    # assert quad in range(1, 5)
    b = (ccd - 1) * 4
    rc = b + quad - 1
    return rc


filters = {"zg": 1, "zr": 2, "zi": 3}


def process_file(
    _file,
    _collections,
    _batch_size=2048,
    _keep_all=False,
    _rm_file=False,
    verbose=False,
    _dry_run=False,
):

    # connect to MongoDB:
    if verbose:
        print("Connecting to DB")
    _client, _db = connect_to_db()
    if verbose:
        print("Successfully connected")

    if verbose:
        print(f"processing {_file}")

    try:
        with tables.open_file(_file, "r+") as f:
            # print(f.root['/matches'].attrs)
            group = f.root.matches
            # print(f.root.matches.exposures._v_attrs)
            # print(f.root.matches.sources._v_attrs)
            # print(f.root.matches.sourcedata._v_attrs)

            ff_basename = os.path.basename(_file)

            # base id:
            _, field, filt, ccd, quad, _ = ff_basename.split("_")
            field = int(field)
            filt = filters[filt]
            ccd = int(ccd[1:])
            quad = int(quad[1:])

            rc = ccd_quad_2_rc(ccd=ccd, quad=quad)
            baseid = int(1e13 + field * 1e9 + rc * 1e7 + filt * 1e6)
            if verbose:
                # print(f'{_file}: {field} {filt} {ccd} {quad}')
                print(f"{_file}: baseid {baseid}")
            exp_baseid = int(1e16 + field * 1e12 + rc * 1e10 + filt * 1e9)

            def clean_up_doc(doc, _keep_all):
                if not _keep_all:
                    # refmagerr = 1.0857/refsnr
                    sources_fields_to_keep = (
                        "meanmag",
                        "percentiles",
                        "vonneumannratio",
                        "dec",
                        "matchid",
                        "nobs",
                        "ra",
                        "refchi",
                        "refmag",
                        "refmagerr",
                        "refsharp",
                        "data",
                    )

                    doc_keys = list(doc.keys())
                    for kk in doc_keys:
                        if kk not in sources_fields_to_keep:
                            doc.pop(kk)

                # convert types for pymongo:
                for k, v in doc.items():
                    if k != "data":
                        if np.issubdtype(type(v), np.integer):
                            doc[k] = int(doc[k])
                        if np.issubdtype(type(v), np.inexact):
                            doc[k] = float(doc[k])
                            if k not in ("ra", "dec"):
                                doc[k] = round(doc[k], 3)
                        # convert numpy arrays into lists
                        if type(v) == np.ndarray:
                            doc[k] = doc[k].tolist()

                # generate unique _id:
                doc["_id"] = baseid + doc["matchid"]

                # from Frank Masci: compute ObjectID, same as serial key in ZTF Objects DB table in IRSA.
                # oid = ((fieldid * 100000 + fid * 10000 + ccdid * 100 + qid * 10) * 10 ** 7) + int(matchid)

                # doc["iqr"] = doc["percentiles"][8] - doc["percentiles"][3]
                # doc["iqr"] = round(doc["iqr"], 3)
                # doc.pop("percentiles")

                doc["filter"] = filt
                doc["field"] = field
                doc["ccd"] = ccd
                doc["quad"] = quad
                doc["rc"] = rc

                # GeoJSON for 2D indexing
                doc["coordinates"] = {}
                _ra = doc["ra"]
                _dec = doc["dec"]
                _radec_str = [deg2hms(_ra), deg2dms(_dec)]
                doc["coordinates"]["radec_str"] = _radec_str
                # for GeoJSON, must be lon:[-180, 180], lat:[-90, 90] (i.e. in deg)
                _radec_geojson = [_ra - 180.0, _dec]
                doc["coordinates"]["radec_geojson"] = {
                    "type": "Point",
                    "coordinates": _radec_geojson,
                }
                doc_data = doc["data"]
                if not _keep_all:
                    # do not store all fields to save space
                    sourcedata_fields_to_keep = (
                        "catflags",
                        "chi",
                        "dec",
                        "expid",
                        "hjd",
                        "mag",
                        "magerr",
                        "programid",
                        "ra",  # 'relphotflags', 'snr',
                        "sharp",
                    )
                    doc_keys = list(doc_data[0].keys())
                    for ddi, ddp in enumerate(doc["data"]):
                        for kk in doc_keys:
                            if kk not in sourcedata_fields_to_keep:
                                doc["data"][ddi].pop(kk)

                for dd in doc["data"]:
                    # convert types for pymongo:
                    for k, v in dd.items():
                        # types.add(type(v))
                        if np.issubdtype(type(v), np.integer):
                            dd[k] = int(dd[k])
                        if np.issubdtype(type(v), np.inexact):
                            dd[k] = float(dd[k])
                            if k not in ("ra", "dec", "hjd"):
                                dd[k] = round(dd[k], 3)
                            elif k == "hjd":
                                dd[k] = round(dd[k], 5)
                        # convert numpy arrays into lists
                        if type(v) == np.ndarray:
                            dd[k] = dd[k].tolist()

                    # generate unique exposure id's that match _id's in exposures collection
                    dd["uexpid"] = exp_baseid + dd["expid"]

                return doc

            exposures = pd.DataFrame.from_records(group.exposures[:])
            # prepare docs to ingest into db:
            docs_exposures = []
            for index, row in exposures.iterrows():
                try:
                    doc = row.to_dict()

                    # unique exposure id:
                    doc["_id"] = exp_baseid + doc["expid"]
                    # print(exp_baseid, doc['expid'], doc['_id'])

                    doc["matchfile"] = ff_basename
                    doc["filter"] = filt
                    doc["field"] = field
                    doc["ccd"] = ccd
                    doc["quad"] = quad
                    doc["rc"] = rc
                    # pprint(doc)
                    docs_exposures.append(doc)
                except Exception as e_:
                    print(str(e_))

            # ingest exposures in one go:
            if not _dry_run:
                if verbose:
                    print(f"ingesting exposures for {_file}")
                insert_multiple_db_entries(
                    _db,
                    _collection=_collections["exposures"],
                    _db_entries=docs_exposures,
                )
                if verbose:
                    print(f"done ingesting exposures for {_file}")

            docs_sources = []
            batch_num = 1
            # fixme? skip transients
            # for source_type in ('source', 'transient'):
            for source_type in ("source",):
                sources_colnames = group[f"{source_type}s"].colnames
                sources = pd.DataFrame.from_records(
                    group[f"{source_type}s"].read(),
                    index="matchid",
                    exclude=[
                        "nabovemeanbystd",
                        "nbelowmeanbystd",
                        "nconsecabovemeanbystd",
                        "nconsecbelowmeanbystd",
                        "nconsecfrommeanbystd",
                        "percentiles",
                    ],
                )

                sourcedatas = pd.DataFrame.from_records(
                    group[f"{source_type}data"][:], index="matchid"
                )
                sourcedatas.rename(
                    columns={"ra": "ra_data", "dec": "dec_data"}, inplace=True
                )
                sourcedata_colnames = sourcedatas.columns.values
                merged = sources.merge(sourcedatas, left_index=True, right_index=True)
                prev_matchid = None
                current_doc = None
                for row in merged.itertuples():
                    matchid = row[0]
                    try:
                        # At a new source
                        if matchid != prev_matchid:
                            # Done with last source; save
                            if current_doc is not None:
                                current_doc = clean_up_doc(current_doc, _keep_all)
                                docs_sources.append(current_doc)

                            # Set up new doc
                            doc = dict(row._asdict())
                            doc["matchid"] = doc["Index"]
                            doc.pop("Index")
                            # Coerce the source data info into its own nested array
                            first_data_row = {}
                            for col in sourcedata_colnames:
                                if col not in ["dec_data", "ra_data"]:
                                    first_data_row[col] = doc[col]
                                else:
                                    real_col = col.split("_data")[0]
                                    first_data_row[real_col] = doc[col]
                                doc.pop(col)
                            doc["data"] = [first_data_row]
                            current_doc = doc
                        # For continued source, just append new data row
                        else:
                            data_row = {}
                            data = dict(row._asdict())
                            for col in sourcedata_colnames:
                                if col not in ["dec_data", "ra_data"]:
                                    data_row[col] = data[col]
                                else:
                                    real_col = col.split("_data")[0]
                                    data_row[real_col] = data[col]

                            current_doc["data"].append(data_row)

                        prev_matchid = matchid

                    except Exception as e_:
                        print(str(e_))

                    # ingest in batches
                    try:
                        if (
                            len(docs_sources) % _batch_size == 0
                            and len(docs_sources) != 0
                        ):
                            if verbose:
                                print(f"inserting batch #{batch_num} for {_file}")
                            if not _dry_run:
                                insert_multiple_db_entries(
                                    _db,
                                    _collection=_collections["sources"],
                                    _db_entries=docs_sources,
                                    _verbose=False,
                                )
                            # flush:
                            docs_sources = []
                            batch_num += 1
                    except Exception as e_:
                        print(str(e_))

        # Clean up and append the last doc
        current_doc = clean_up_doc(current_doc, _keep_all)
        docs_sources.append(current_doc)
        # ingest remaining
        while len(docs_sources) > 0:
            try:
                # In case mongo crashed and disconnected, docs will accumulate in documents
                # keep on trying to insert them until successful
                if verbose:
                    print(f"inserting batch #{batch_num} for {_file}")
                if not _dry_run:
                    insert_multiple_db_entries(
                        _db,
                        _collection=_collections["sources"],
                        _db_entries=docs_sources,
                        _verbose=False,
                    )
                    # flush:
                    docs_sources = []

            except Exception as e:
                traceback.print_exc()
                print(e)
                print("Failed, waiting 5 seconds to retry")
                time.sleep(5)

    except Exception as e:
        traceback.print_exc()
        print(e)

    # disconnect from db:
    try:
        if _rm_file:
            os.remove(_file)
            if verbose:
                print(f"Successfully removed {_file}")
        _client.close()
        if verbose:
            if verbose:
                print("Successfully disconnected from db")
    finally:
        pass


if __name__ == "__main__":
    """ Create command line argument parser """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=""
    )

    parser.add_argument(
        "--keepall", action="store_true", help="keep all fields from the matchfiles?"
    )
    parser.add_argument(
        "--rm", action="store_true", help="remove matchfiles after ingestion?"
    )
    parser.add_argument("--dryrun", action="store_true", help="dry run?")
    parser.add_argument(
        "--np", type=int, default=96, help="number of processes for parallel ingestion"
    )
    parser.add_argument("--bs", type=int, default=2048, help="batch size for ingestion")
    parser.add_argument(
        "--tag", type=str, default="20200401", help="matchfile release time tag"
    )

    args = parser.parse_args()

    dry_run = args.dryrun
    keep_all = args.keepall
    rm_file = args.rm

    # connect to MongoDB:
    print("Connecting to DB")
    client, db = connect_to_db()
    print("Successfully connected")

    # t_tag = '20181220'
    # t_tag = '20190412'
    # t_tag = '20190614'
    # t_tag = '20190718'
    # t_tag = '20191101'
    # t_tag = '20200401'
    t_tag = args.tag

    collections = {
        "exposures": f"ZTF_exposures_{t_tag}",
        "sources": f"ZTF_sources_{t_tag}",
    }

    # create indices:
    print("Creating indices")
    if not dry_run:
        db[collections["exposures"]].create_index(
            [("expid", pymongo.ASCENDING)], background=True
        )
        db[collections["sources"]].create_index(
            [("coordinates.radec_geojson", "2dsphere"), ("_id", pymongo.ASCENDING)],
            background=True,
        )
        db[collections["sources"]].create_index(
            [
                ("field", pymongo.ASCENDING),
                ("ccd", pymongo.ASCENDING),
                ("quad", pymongo.ASCENDING),
            ],
            background=True,
        )
        db[collections["sources"]].create_index(
            [("nobs", pymongo.ASCENDING), ("_id", pymongo.ASCENDING)], background=True
        )
        # db[collections['sources']].create_index([('data.programid', pymongo.ASCENDING)], background=True)
        # db[collections['sources']].create_index([('data.expid', pymongo.ASCENDING)], background=True)

    # number of records to insert
    batch_size = args.bs
    # batch_size = 1

    _location = f"/_tmp/ztf_matchfiles_{t_tag}/"
    files = glob.glob(os.path.join(_location, "ztf_*.pytable"))

    print(f"# files to process: {len(files)}")

    input_list = [
        [f, collections, batch_size, keep_all, rm_file, True, dry_run]
        for f in sorted(files)
    ]
    # for a more even job distribution:
    random.shuffle(input_list)

    with mp.Pool(processes=args.np) as p:
        list(tqdm(p.starmap(process_file, input_list), total=len(files)))

    print(f"All done")