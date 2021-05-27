#!/usr/bin/env python
# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import os
from functools import partial
from pandas import DataFrame
from tqdm import tqdm

path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(path)

from lib.cast import isna
from lib.concurrent import thread_map
from lib.constants import SRC, GCS_BUCKET_TEST, GCS_BUCKET_PROD
from lib.data_source import DataSource
from lib.io import export_csv, read_table, temporary_directory
from lib.gcloud import get_storage_bucket
from lib.memory_efficient import get_table_columns, table_read_column
from lib.pipeline import DataPipeline
from lib.pipeline_tools import get_pipelines
from typing import List, Iterable, Dict


def download_file(bucket_name: str, remote_path: str, local_path: str) -> None:
    bucket = get_storage_bucket(bucket_name)
    # print(f"Downloading {remote_path} to {local_path}")
    return bucket.blob(remote_path).download_to_filename(str(local_path))


def read_source_output(data_pipeline: DataPipeline, data_source: DataSource) -> DataFrame:
    with temporary_directory() as workdir:
        output_path = workdir / f"{data_source.uuid(data_pipeline.table)}.csv"
        try:
            download_file(GCS_BUCKET_TEST, f"intermediate/{output_path.name}", output_path)
            columns = get_table_columns(output_path)
            dates = list(table_read_column(output_path, "date")) if "date" in columns else [None]
            return {
                "pipeline": data_pipeline.name,
                "data_source": f"{data_source.__module__}.{data_source.name}",
                "columns": ",".join(columns),
                "first_date": min(dates),
                "last_date": max(dates),
                "location_keys": ",".join(sorted(set(table_read_column(output_path, "key")))),
            }
        except Exception as exc:
            print(exc, file=sys.stderr)
            return {}


def get_source_outputs(data_pipelines: Iterable[DataPipeline]) -> Iterable[Dict]:
    """Map a list of pipeline names to their source configs."""

    for data_pipeline in tqdm(list(data_pipelines), desc="Processing data pipelines"):
        # print(f"Processing {data_pipeline.name}")
        map_iter = data_pipeline.data_sources
        map_func = partial(read_source_output, data_pipeline)
        map_opts = dict(desc="Downloading data tables", leave=False)
        yield from thread_map(map_func, map_iter, **map_opts)


def get_combined_table_sources(table_name: str):
    """ Publishes a table with the data source of each data point """

    pipeline = DataPipeline.load(table_name.replace("_", "-"))

    with temporary_directory() as workdir:
        workdir = SRC / ".." / "output" / "tmp"

        # Download the combined table and all the intermediate files used to create it
        print("downloading combined table")
        output_path = workdir / f"{pipeline.table}.csv"
        # download_file(GCS_BUCKET_PROD, f"v2/{pipeline.table}.csv", output_path)
        combined_table = read_table(output_path)
        index_columns = ["key"] + (["date"] if "date" in combined_table.columns else [])
        combined_table.set_index(index_columns, inplace=True)

        intermediate_tables = []
        for data_source in tqdm(pipeline.data_sources, desc="downloading intermediate tables"):
            fname = data_source.uuid(pipeline.table) + ".csv"
            try:
                # download_file(GCS_BUCKET_TEST, f"intermediate/{fname}", workdir / fname)
                table = read_table(workdir / fname).groupby(index_columns).last()
                # intermediate_tables.insert(0, (data_source, table))
                intermediate_tables.append((data_source, table))
            except Exception as exc:
                print(f"intermediate table not found: {fname}", file=sys.stderr)

        # Iterate over the indices for each column independently
        source_map = {idx: {} for idx in combined_table.index}
        # combined_table = combined_table.iloc[:1000]
        for data_source, table in tqdm(intermediate_tables, desc="Building index map"):
            for idx, row in table.iterrows():
                if idx not in source_map:
                    # If the intermediate table was *just* updated some values may not be found in
                    # the combined table yet.
                    continue
                for col in filter(lambda col: not isna(row[col]), table.columns):
                    source_map[idx][col] = data_source.name
        source_table = DataFrame(source_map.values(), index=source_map.keys())

        # Iterate over the indices for each column independently
        # source_map: List[Dict[str, str]] = []
        # combined_table = combined_table.iloc[:1000]
        # map_opts = dict(total=len(combined_table), desc="Records")
        # for idx, record in tqdm(combined_table.iterrows(), **map_opts):
        #     record_sources: Dict[str, str] = {}
        #     for col in combined_table.columns:
        #         value = record[col]
        #         if isna(value):
        #             # If the record is NaN, data source is NaN too
        #             # Technically a data source could output NaN values, but we don't care here
        #             record_sources[col] = None
        #         else:
        #             # Otherwise iterate over each intermediate result in order until a match
        #             # for the value is found
        #             for data_source, table in intermediate_tables:
        #                 if col not in table.columns:
        #                     continue
        #                 if not table.index.isin([idx]).any():
        #                     continue
        #                 if table.loc[idx, col] == value:
        #                     # if table.loc[idx, col] == value:
        #                     record_sources[col] = data_source.name
        #                     break

        #     source_map.append(record_sources)

        # Create a table with the source map
        # source_table = DataFrame(source_map, index=combined_table.index)
        export_csv(source_table.reset_index(), SRC / f"{pipeline.table}.sources.csv")


if __name__ == "__main__":
    # To authenticate with Cloud locally, run the following commands:
    # > $env:GOOGLE_CLOUD_PROJECT = "github-open-covid-19"
    # > $env:GCS_SERVICE_ACCOUNT = "github-open-covid-19@appspot.gserviceaccount.com"
    # > $env:GCP_TOKEN = $(gcloud auth application-default print-access-token)
    # results = DataFrame(get_source_outputs(get_pipelines()))
    # print(results.to_csv(index=False))
    get_combined_table_sources("epidemiology")
