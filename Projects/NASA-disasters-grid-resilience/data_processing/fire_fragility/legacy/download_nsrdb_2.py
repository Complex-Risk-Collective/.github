#!/usr/bin/env python3

"""
Download NSRDB GOES Aggregated PSM v4 data through the NLR REST API.

This script does not use HSDS or h5pyd. It therefore does not require
HS_ENDPOINT. It can reuse hs_api_key from ~/.hscfg, but the HSDS
endpoint in that file is ignored.

Retrieval behavior
------------------
POINT + one year:
    Direct CSV download is available.

POINT + multiple years:
    Asynchronous JSON archive request.

MULTIPOINT or POLYGON:
    Asynchronous JSON archive request.

For asynchronous requests, the JSON response acknowledges that file
generation has started. NLR sends a download link to the supplied email
address when the archive is ready.

Documentation
-------------
https://developer.nlr.gov/docs/solar/nsrdb/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import requests


API_ROOT = "https://developer.nlr.gov/api/nsrdb/v2"

DATASET = "nsrdb-GOES-aggregated-v4-0-0"

DOWNLOAD_ENDPOINT = (
    f"{API_ROOT}/solar/"
    f"{DATASET}-download"
)

SITE_COUNT_ENDPOINT = (
    f"{API_ROOT}/site-count.json"
)

MIN_YEAR = 1998
MAX_YEAR = 2025

AVAILABLE_ATTRIBUTES = (
    "air_temperature",
    "alpha",
    "aod",
    "asymmetry",
    "clearsky_dhi",
    "clearsky_dni",
    "clearsky_ghi",
    "cloud_fill_flag",
    "cloud_type",
    "dew_point",
    "dhi",
    "dni",
    "fill_flag",
    "ghi",
    "ozone",
    "relative_humidity",
    "solar_zenith_angle",
    "ssa",
    "surface_albedo",
    "surface_pressure",
    "total_precipitable_water",
    "wind_direction",
    "wind_speed",
)

# These fields support the wildfire-weather and conductor-rating
# workflows without requesting every available NSRDB attribute.
DEFAULT_ATTRIBUTES = (
    "air_temperature",
    "dew_point",
    "relative_humidity",
    "surface_pressure",
    "wind_speed",
    "wind_direction",
    "ghi",
    "dni",
    "dhi",
    "solar_zenith_angle",
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Download GOES Aggregated PSM v4 NSRDB data "
            "through the NLR REST API."
        )
    )

    location_group = parser.add_mutually_exclusive_group(
        required=True
    )

    location_group.add_argument(
        "--point",
        nargs=2,
        type=float,
        metavar=("LONGITUDE", "LATITUDE"),
        help=(
            "Retrieve the NSRDB site nearest the supplied "
            "longitude and latitude."
        ),
    )

    location_group.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help=(
            "Retrieve all NSRDB sites intersecting the supplied "
            "geographic bounding box."
        ),
    )

    location_group.add_argument(
        "--wkt",
        help=(
            "POINT, MULTIPOINT, or POLYGON geometry as "
            "well-known text in longitude/latitude coordinates."
        ),
    )

    parser.add_argument(
        "--years",
        nargs="+",
        required=True,
        help=(
            "One or more years, for example --years 2023 2024. "
            "Comma-separated years are also accepted. Use 'all' "
            "to request every currently documented year."
        ),
    )

    parser.add_argument(
        "--attributes",
        nargs="+",
        default=list(DEFAULT_ATTRIBUTES),
        help=(
            "NSRDB attributes separated by spaces or commas. "
            "Use 'all' to request all documented attributes."
        ),
    )

    parser.add_argument(
        "--interval",
        type=int,
        choices=(30, 60),
        default=60,
        help=(
            "Temporal interval in minutes. Allowed values are "
            "30 or 60. Default: 60."
        ),
    )

    time_group = parser.add_mutually_exclusive_group()

    time_group.add_argument(
        "--utc",
        dest="utc",
        action="store_true",
        help="Return timestamps in UTC. This is the default.",
    )

    time_group.add_argument(
        "--local-time",
        dest="utc",
        action="store_false",
        help=(
            "Return local standard time without daylight-saving "
            "adjustment."
        ),
    )

    parser.set_defaults(utc=True)

    parser.add_argument(
        "--leap-day",
        action="store_true",
        help="Include February 29 in leap years.",
    )

    parser.add_argument(
        "--email",
        required=True,
        help=(
            "Active email address used by NLR to deliver "
            "asynchronous archive links."
        ),
    )

    parser.add_argument(
        "--full-name",
        help="Optional name of the person requesting the data.",
    )

    parser.add_argument(
        "--affiliation",
        help="Optional organization or affiliation.",
    )

    parser.add_argument(
        "--reason",
        help="Optional reason for requesting the data.",
    )

    parser.add_argument(
        "--mailing-list",
        action="store_true",
        help=(
            "Subscribe the supplied email address to the "
            "NSRDB mailing list."
        ),
    )

    parser.add_argument(
        "--format",
        dest="response_format",
        choices=("auto", "csv", "json"),
        default="auto",
        help=(
            "Response format. Auto uses direct CSV for one point "
            "and one year, and asynchronous JSON otherwise."
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Output CSV filename for a direct download or JSON "
            "filename for an asynchronous request acknowledgement."
        ),
    )

    parser.add_argument(
        "--api-key",
        help=(
            "NLR developer API key. This overrides environment "
            "variables and configuration-file values."
        ),
    )

    parser.add_argument(
        "--hscfg",
        default="~/.hscfg",
        help=(
            "Optional .hscfg file from which hs_api_key can be "
            "reused. hs_endpoint is ignored. Default: ~/.hscfg"
        ),
    )

    parser.add_argument(
        "--skip-site-count",
        action="store_true",
        help=(
            "Skip the preliminary site-count request for polygon "
            "or multipoint geometry."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="HTTP request timeout in seconds. Default: 300.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output file.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate and display the request without submitting "
            "the download request."
        ),
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print additional request diagnostics. The API key "
            "is never printed."
        ),
    )

    return parser.parse_args()


def meaningful(value):
    """
    Return a stripped string or None for empty/null-like values.
    """
    if value is None:
        return None

    value = str(value).strip().strip("'\"")

    if value.lower() in {
        "",
        "none",
        "null",
    }:
        return None

    return value


def read_sectionless_config(path):
    """
    Read a sectionless key=value configuration file.

    This supports reusing hs_api_key from an existing .hscfg file.
    The HSDS endpoint is intentionally ignored.
    """
    values = {}

    if not path.is_file():
        return values

    lines = path.read_text(
        encoding="utf-8"
    ).splitlines()

    for line_number, raw_line in enumerate(
        lines,
        start=1,
    ):
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith(("#", ";")):
            continue

        if "=" not in line:
            raise ValueError(
                f"Invalid configuration line {line_number} "
                f"in {path}: {raw_line!r}"
            )

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip().lower()
        value = meaningful(value)

        if value is not None:
            values[key] = value

    return values


def resolve_api_key(
    cli_api_key,
    hscfg_path,
):
    """
    Resolve the NLR developer API key.

    Precedence:
        1. --api-key
        2. NLR_API_KEY
        3. NSRDB_API_KEY
        4. API_KEY
        5. HS_API_KEY
        6. nlr_api_key in .hscfg
        7. nsrdb_api_key in .hscfg
        8. api_key in .hscfg
        9. hs_api_key in .hscfg
    """
    value = meaningful(cli_api_key)

    if value:
        return value, "command line"

    environment_variables = (
        "NLR_API_KEY",
        "NSRDB_API_KEY",
        "API_KEY",
        "HS_API_KEY",
    )

    for variable in environment_variables:
        value = meaningful(
            os.environ.get(variable)
        )

        if value:
            return (
                value,
                f"environment variable {variable}",
            )

    config_path = Path(
        hscfg_path
    ).expanduser().resolve()

    config = read_sectionless_config(
        config_path
    )

    config_keys = (
        "nlr_api_key",
        "nsrdb_api_key",
        "api_key",
        "hs_api_key",
    )

    for key_name in config_keys:
        value = meaningful(
            config.get(key_name)
        )

        if value:
            return (
                value,
                f"{key_name} in {config_path}",
            )

    raise RuntimeError(
        "No NLR developer API key was found.\n"
        "Supply --api-key, set NLR_API_KEY or NSRDB_API_KEY, "
        f"or define hs_api_key in {config_path}."
    )


def split_values(
    values: Iterable[str],
):
    """
    Split command-line values supplied with spaces, commas, or both.
    """
    result = []

    for value in values:
        for item in str(value).split(","):
            item = item.strip()

            if item:
                result.append(item)

    return result


def parse_years(values):
    """
    Validate and normalize requested NSRDB years.
    """
    values = split_values(values)

    if (
        len(values) == 1
        and values[0].lower() == "all"
    ):
        return list(
            range(
                MIN_YEAR,
                MAX_YEAR + 1,
            )
        )

    years = []

    for value in values:
        try:
            year = int(value)
        except ValueError as error:
            raise ValueError(
                f"Invalid NSRDB year: {value!r}"
            ) from error

        if not MIN_YEAR <= year <= MAX_YEAR:
            raise ValueError(
                f"Year {year} is outside the documented "
                f"GOES Aggregated range "
                f"{MIN_YEAR}-{MAX_YEAR}."
            )

        if year not in years:
            years.append(year)

    if not years:
        raise ValueError(
            "At least one year must be supplied."
        )

    return sorted(years)


def parse_attributes(values):
    """
    Validate and normalize requested NSRDB attributes.
    """
    attributes = split_values(values)

    if (
        len(attributes) == 1
        and attributes[0].lower() == "all"
    ):
        return list(
            AVAILABLE_ATTRIBUTES
        )

    invalid = sorted(
        set(attributes)
        - set(AVAILABLE_ATTRIBUTES)
    )

    if invalid:
        raise ValueError(
            "Unsupported GOES Aggregated attribute(s): "
            + ", ".join(invalid)
            + "\n\nAvailable attributes:\n  "
            + "\n  ".join(AVAILABLE_ATTRIBUTES)
        )

    unique = []

    for attribute in attributes:
        if attribute not in unique:
            unique.append(attribute)

    if not unique:
        raise ValueError(
            "At least one attribute must be requested."
        )

    return unique


def validate_longitude(value):
    if not -180.0 <= value <= 180.0:
        raise ValueError(
            "Longitude must be within [-180, 180]: "
            f"{value}"
        )


def validate_latitude(value):
    if not -90.0 <= value <= 90.0:
        raise ValueError(
            "Latitude must be within [-90, 90]: "
            f"{value}"
        )


def create_point_wkt(
    longitude,
    latitude,
):
    """
    Create longitude-first POINT WKT.
    """
    validate_longitude(longitude)
    validate_latitude(latitude)

    return (
        f"POINT("
        f"{longitude:.10f} "
        f"{latitude:.10f}"
        f")"
    )


def create_bbox_wkt(
    west,
    south,
    east,
    north,
):
    """
    Convert west/south/east/north bounds to closed polygon WKT.
    """
    validate_longitude(west)
    validate_longitude(east)
    validate_latitude(south)
    validate_latitude(north)

    if west >= east:
        raise ValueError(
            "West must be less than east. "
            "Dateline-crossing regions must be divided "
            "into separate requests."
        )

    if south >= north:
        raise ValueError(
            "South must be less than north."
        )

    return (
        "POLYGON(("
        f"{west:.10f} {south:.10f},"
        f"{east:.10f} {south:.10f},"
        f"{east:.10f} {north:.10f},"
        f"{west:.10f} {north:.10f},"
        f"{west:.10f} {south:.10f}"
        "))"
    )


def determine_wkt(args):
    """
    Resolve --point, --bbox, or --wkt into a scalar WKT string.
    """
    if args.point is not None:
        longitude, latitude = args.point

        wkt = create_point_wkt(
            longitude,
            latitude,
        )

    elif args.bbox is not None:
        west, south, east, north = args.bbox

        wkt = create_bbox_wkt(
            west,
            south,
            east,
            north,
        )

    else:
        wkt = meaningful(args.wkt)

        if wkt is None:
            raise ValueError(
                "The supplied WKT is empty."
            )

    if not isinstance(wkt, str):
        raise TypeError(
            "WKT must be a string, not "
            f"{type(wkt).__name__}."
        )

    wkt = wkt.strip()

    if not re.match(
        r"^(POINT|MULTIPOINT|POLYGON)\s*(Z\s*)?\(",
        wkt,
        flags=re.IGNORECASE,
    ):
        raise ValueError(
            "--wkt must contain a POINT, MULTIPOINT, "
            "or POLYGON geometry."
        )

    return wkt


def geometry_type(wkt):
    """
    Return POINT, MULTIPOINT, or POLYGON.
    """
    match = re.match(
        r"^\s*([A-Za-z]+)",
        wkt,
    )

    if not match:
        raise ValueError(
            "Could not determine the WKT geometry type."
        )

    return match.group(1).upper()


def validate_email(email):
    """
    Perform basic validation for a plain email address.

    Markdown mailto syntax is rejected.
    """
    email = meaningful(email)

    if email is None:
        raise ValueError(
            "An email address is required."
        )

    if email.startswith("[") or "mailto:" in email.lower():
        raise ValueError(
            "Pass only the plain email address, not Markdown "
            "mailto syntax. Example: user@example.com"
        )

    if not re.match(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        email,
    ):
        raise ValueError(
            f"Invalid email address: {email!r}"
        )

    return email


def determine_response_format(
    requested_format,
    wkt,
    years,
):
    """
    Choose direct CSV or asynchronous JSON.

    Direct CSV is restricted by the API to one POINT and one year.
    """
    wkt_type = geometry_type(wkt)

    if requested_format == "auto":
        if (
            wkt_type == "POINT"
            and len(years) == 1
        ):
            response_format = "csv"
        else:
            response_format = "json"
    else:
        response_format = requested_format

    if response_format == "csv":
        if wkt_type != "POINT":
            raise ValueError(
                "Direct CSV retrieval supports only one POINT. "
                "Use --format json for a polygon or multipoint."
            )

        if len(years) != 1:
            raise ValueError(
                "Direct CSV retrieval supports only one year. "
                "Use --format json for multiple years."
            )

    return response_format


def prepare_output_path(
    output_value,
    response_format,
    overwrite,
):
    """
    Validate the requested output filename.
    """
    output_path = Path(
        output_value
    ).expanduser().resolve()

    expected_suffix = (
        ".csv"
        if response_format == "csv"
        else ".json"
    )

    if output_path.suffix.lower() != expected_suffix:
        raise ValueError(
            f"The selected {response_format} response requires "
            f"an output filename ending in {expected_suffix}."
        )

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}\n"
            "Use --overwrite to replace it."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    return output_path


def build_payload(
    args,
    years,
    attributes,
    wkt,
    email,
):
    """
    Build scalar form parameters for the NLR API.
    """
    payload = {
        "wkt": str(wkt),
        "attributes": ",".join(attributes),
        "names": ",".join(
            str(year)
            for year in years
        ),
        "utc": str(args.utc).lower(),
        "leap_day": str(
            args.leap_day
        ).lower(),
        "interval": str(args.interval),
        "email": email,
        "mailing_list": str(
            args.mailing_list
        ).lower(),
    }

    optional_fields = {
        "full_name": args.full_name,
        "affiliation": args.affiliation,
        "reason": args.reason,
    }

    for name, value in optional_fields.items():
        value = meaningful(value)

        if value is not None:
            payload[name] = value

    return payload


def encode_payload(payload):
    """
    Explicitly URL-encode the form data.

    This is intentionally returned as a string rather than passing
    a dictionary to requests. It ensures that the WKT reaches the API
    as one application/x-www-form-urlencoded scalar field.
    """
    cleaned = {}

    for key, value in payload.items():
        if value is None:
            continue

        if isinstance(value, (list, tuple, dict, set)):
            raise TypeError(
                f"Form field {key!r} must be scalar, not "
                f"{type(value).__name__}."
            )

        cleaned[str(key)] = str(value)

    return urlencode(
        cleaned,
        doseq=False,
        safe="(),",
    )


def request_error_message(
    title,
    response,
    submitted_wkt=None,
):
    """
    Format detailed API errors without exposing the API key.
    """
    redacted_url = re.sub(
        r"(api_key=)[^&]+",
        r"\1REDACTED",
        response.url,
        flags=re.IGNORECASE,
    )

    parts = [
        title,
        f"HTTP status: {response.status_code}",
        f"Request URL: {redacted_url}",
    ]

    if submitted_wkt is not None:
        parts.append(
            f"Submitted WKT: {submitted_wkt!r}"
        )

    parts.append(
        "Response:\n"
        + response.text[:5000]
    )

    return "\n".join(parts)


def call_site_count(
    session,
    api_key,
    wkt,
    timeout,
    debug=False,
):
    """
    Query the site-count endpoint.

    The WKT is passed as a URL query parameter for this GET request.
    """
    parameters = {
        "api_key": api_key,
        "wkt": str(wkt),
    }

    if debug:
        print(
            "Calling site-count endpoint:"
        )
        print(
            f"  Endpoint: {SITE_COUNT_ENDPOINT}"
        )
        print(
            f"  WKT: {wkt!r}"
        )

    response = session.get(
        SITE_COUNT_ENDPOINT,
        params=parameters,
        timeout=timeout,
    )

    if not response.ok:
        raise RuntimeError(
            request_error_message(
                "NSRDB site-count request failed.",
                response,
                submitted_wkt=wkt,
            )
        )

    try:
        result = response.json()
    except ValueError as error:
        raise RuntimeError(
            request_error_message(
                "The site-count endpoint returned "
                "a non-JSON response.",
                response,
                submitted_wkt=wkt,
            )
        ) from error

    errors = result.get("errors") or []

    if errors:
        raise RuntimeError(
            "NSRDB site-count request was rejected.\n"
            f"Submitted WKT: {wkt!r}\n"
            "Errors:\n"
            + "\n".join(
                str(item)
                for item in errors
            )
            + "\nFull response:\n"
            + json.dumps(
                result,
                indent=2,
            )
        )

    outputs = result.get("outputs") or {}

    site_count = outputs.get(DATASET)

    if site_count is None:
        available_keys = sorted(
            outputs.keys()
        )

        raise RuntimeError(
            "The site-count response did not contain the "
            f"expected dataset key {DATASET!r}.\n"
            "Available output keys:\n  "
            + "\n  ".join(available_keys)
        )

    try:
        site_count = int(site_count)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "The site-count service returned a non-integer "
            f"value for {DATASET}: {site_count!r}"
        ) from error

    if site_count <= 0:
        raise RuntimeError(
            "The supplied geometry does not intersect any "
            "GOES Aggregated PSM v4 sites."
        )

    return site_count


def save_json_response(
    result,
    output_path,
):
    """
    Save a formatted JSON API response.
    """
    output_path.write_text(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def submit_archive_request(
    session,
    api_key,
    payload,
    output_path,
    timeout,
    debug=False,
):
    """
    Submit an asynchronous archive-generation request.

    The API key remains in the URL query string. All other parameters,
    including WKT, are placed in an explicitly URL-encoded POST body.
    """
    url = (
        f"{DOWNLOAD_ENDPOINT}.json"
    )

    encoded_payload = encode_payload(
        payload
    )

    headers = {
        "Content-Type": (
            "application/x-www-form-urlencoded"
        ),
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "nsrdb-rest-downloader/1.0"
        ),
    }

    if debug:
        print(
            "Submitting asynchronous NSRDB request:"
        )
        print(f"  Endpoint: {url}")
        print(
            f"  Submitted WKT: "
            f"{payload['wkt']!r}"
        )
        print(
            "  Encoded form body:"
        )
        print(
            "  "
            + encoded_payload
        )

    response = session.post(
        url,
        params={
            "api_key": api_key,
        },
        data=encoded_payload,
        headers=headers,
        timeout=timeout,
    )

    if not response.ok:
        raise RuntimeError(
            request_error_message(
                "NSRDB archive request failed.",
                response,
                submitted_wkt=payload.get("wkt"),
            )
        )

    try:
        result = response.json()
    except ValueError as error:
        raise RuntimeError(
            request_error_message(
                "The NSRDB archive service returned "
                "a non-JSON response.",
                response,
                submitted_wkt=payload.get("wkt"),
            )
        ) from error

    save_json_response(
        result,
        output_path,
    )

    errors = result.get("errors") or []

    if errors:
        server_inputs = (
            result.get("inputs")
            or {}
        )

        raise RuntimeError(
            "The NSRDB archive request was rejected.\n"
            f"Submitted WKT: "
            f"{payload.get('wkt')!r}\n"
            "Server-reported inputs:\n"
            + json.dumps(
                server_inputs,
                indent=2,
            )
            + "\nErrors:\n"
            + "\n".join(
                str(item)
                for item in errors
            )
            + "\n\nThe complete response was saved to:\n"
            + str(output_path)
        )

    outputs = result.get("outputs") or {}

    message = outputs.get("message")

    download_url = (
        outputs.get("downloadUrl")
        or outputs.get("download_url")
    )

    if message:
        print(
            f"Server response: {message}"
        )

    if download_url:
        print(
            "Archive URL reported by the server: "
            f"{download_url}"
        )

    print(
        "The request acknowledgement was saved to: "
        f"{output_path}"
    )


def write_direct_csv(
    session,
    api_key,
    payload,
    output_path,
    timeout,
    debug=False,
):
    """
    Download one point and one year directly as CSV.
    """
    url = (
        f"{DOWNLOAD_ENDPOINT}.csv"
    )

    encoded_payload = encode_payload(
        payload
    )

    headers = {
        "Content-Type": (
            "application/x-www-form-urlencoded"
        ),
        "Accept": (
            "text/csv,application/json"
        ),
        "Cache-Control": "no-cache",
        "User-Agent": (
            "nsrdb-rest-downloader/1.0"
        ),
    }

    if debug:
        print(
            "Submitting direct NSRDB CSV request:"
        )
        print(f"  Endpoint: {url}")
        print(
            f"  Submitted WKT: "
            f"{payload['wkt']!r}"
        )
        print(
            "  Encoded form body:"
        )
        print(
            "  "
            + encoded_payload
        )

    with session.post(
        url,
        params={
            "api_key": api_key,
        },
        data=encoded_payload,
        headers=headers,
        timeout=timeout,
        stream=True,
    ) as response:
        if not response.ok:
            raise RuntimeError(
                request_error_message(
                    "NSRDB CSV request failed.",
                    response,
                    submitted_wkt=payload.get("wkt"),
                )
            )

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if "json" in content_type:
            try:
                error_response = (
                    response.json()
                )
            except ValueError:
                error_response = {
                    "message": (
                        response.text[:5000]
                    )
                }

            raise RuntimeError(
                "The NSRDB CSV endpoint returned JSON "
                "instead of CSV:\n"
                + json.dumps(
                    error_response,
                    indent=2,
                )
            )

        temporary_path = output_path.with_name(
            f"{output_path.name}.part"
        )

        if temporary_path.exists():
            temporary_path.unlink()

        try:
            with temporary_path.open(
                "wb"
            ) as output_file:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024,
                ):
                    if chunk:
                        output_file.write(
                            chunk
                        )

            if (
                not temporary_path.exists()
                or temporary_path.stat().st_size == 0
            ):
                raise RuntimeError(
                    "The NSRDB API returned an empty CSV file."
                )

            # Detect a JSON or HTML error that was returned with
            # an incorrect content-type.
            with temporary_path.open(
                "rb"
            ) as downloaded_file:
                prefix = downloaded_file.read(
                    512
                ).lstrip()

            if prefix.startswith(
                (b"{", b"[")
            ):
                text = temporary_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                raise RuntimeError(
                    "The direct-download endpoint returned JSON "
                    "rather than CSV:\n"
                    + text[:5000]
                )

            if prefix.lower().startswith(
                (b"<!doctype html", b"<html")
            ):
                text = temporary_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                raise RuntimeError(
                    "The direct-download endpoint returned HTML "
                    "rather than CSV:\n"
                    + text[:5000]
                )

            temporary_path.replace(
                output_path
            )

        except Exception:
            if temporary_path.exists():
                temporary_path.unlink()

            raise

    print(
        f"NSRDB CSV written to: {output_path}"
    )


def display_request(
    response_format,
    years,
    attributes,
    wkt,
    email,
    output_path,
    api_key_source,
    site_count=None,
):
    """
    Print a request summary without exposing the API key.
    """
    endpoint = (
        f"{DOWNLOAD_ENDPOINT}."
        f"{response_format}"
    )

    print(f"API endpoint: {endpoint}")
    print(f"API key source: {api_key_source}")
    print(f"Response format: {response_format}")
    print(
        "Years: "
        + ", ".join(
            str(year)
            for year in years
        )
    )
    print(
        "Attributes: "
        + ", ".join(attributes)
    )
    print(
        f"Geometry type: "
        f"{geometry_type(wkt)}"
    )
    print(f"Generated WKT: {wkt!r}")
    print(f"Delivery email: {email}")
    print(f"Output: {output_path}")

    if site_count is not None:
        print(
            f"Intersecting GOES Aggregated sites: "
            f"{site_count:,}"
        )


def main():
    args = parse_arguments()

    email = validate_email(
        args.email
    )

    api_key, api_key_source = resolve_api_key(
        cli_api_key=args.api_key,
        hscfg_path=args.hscfg,
    )

    years = parse_years(
        args.years
    )

    attributes = parse_attributes(
        args.attributes
    )

    wkt = determine_wkt(args)

    response_format = determine_response_format(
        requested_format=args.response_format,
        wkt=wkt,
        years=years,
    )

    output_path = prepare_output_path(
        output_value=args.output,
        response_format=response_format,
        overwrite=args.overwrite,
    )

    payload = build_payload(
        args=args,
        years=years,
        attributes=attributes,
        wkt=wkt,
        email=email,
    )

    session = requests.Session()

    site_count = None

    if (
        not args.skip_site_count
        and geometry_type(wkt)
        in {"POLYGON", "MULTIPOINT"}
    ):
        site_count = call_site_count(
            session=session,
            api_key=api_key,
            wkt=wkt,
            timeout=args.timeout,
            debug=args.debug,
        )

    display_request(
        response_format=response_format,
        years=years,
        attributes=attributes,
        wkt=wkt,
        email=email,
        output_path=output_path,
        api_key_source=api_key_source,
        site_count=site_count,
    )

    if args.dry_run:
        print()
        print(
            "Dry run complete. No download request "
            "was submitted."
        )
        return

    if response_format == "csv":
        write_direct_csv(
            session=session,
            api_key=api_key,
            payload=payload,
            output_path=output_path,
            timeout=args.timeout,
            debug=args.debug,
        )

    else:
        submit_archive_request(
            session=session,
            api_key=api_key,
            payload=payload,
            output_path=output_path,
            timeout=args.timeout,
            debug=args.debug,
        )


if __name__ == "__main__":
    try:
        main()

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        FileExistsError,
    ) as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)

    except KeyboardInterrupt:
        print(
            "\nRequest cancelled.",
            file=sys.stderr,
        )
        sys.exit(130)



