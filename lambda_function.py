
import json
import logging
import re
import boto3
from botocore.exceptions import ClientError
from urllib.parse import unquote_plus

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log(level, message, **kwargs):
    """Emit a structured log entry."""
    entry = {"message": message, **kwargs}
    getattr(logger, level)(json.dumps(entry))

# ── AWS clients (cold and warm starts) ─────────
s3 = boto3.client("s3")
rekognition = boto3.client("rekognition")

# ── Constants ─────────────────────────────────────────────────────────────────

HOUSE_LABELS = {
    "House", "Housing", "Home", "Cottage", "Villa",
    "Building", "Architecture", "Neighborhood", "Suburb",
    "Residential Building", "Single Family Residence",
}
HOUSE_CONFIDENCE_THRESHOLD = 70.0


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
TEXT_EXTENSIONS  = {".txt", ".csv"}

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

ZIP_CODE_RE   = re.compile(r"\b\d{5}(?:-\d{4})?\b")          
STATE_ABBR_RE = re.compile(r"\b([A-Z]{2})\b")                 
US_COUNTRY_RE = re.compile(r"\b(USA|U\.S\.A\.?|United States)\b", re.IGNORECASE)

NON_US_COUNTRIES_RE = re.compile(
    r"\b(UK|United Kingdom|England|France|Germany|Italy|Spain|Japan|China|"
    r"Australia|Canada|Mexico|Brazil|India|Russia|Netherlands|Sweden|Norway|"
    r"Denmark|Switzerland|Austria|Belgium|Portugal|Poland|Greece|Turkey|"
    r"South Korea|Singapore|New Zealand|Ireland|Scotland|Wales)\b",
    re.IGNORECASE,
)


# ── Main ──────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """
    entry point and S3 sends one record per uploaded object, but the event
    can batch multiple records 
    """
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = unquote_plus(record["s3"]["object"]["key"])  

        log("info", "Processing file", bucket=bucket, key=key)

        if key.startswith("Houses/") or key.startswith("Addresses/"):
            log("info", "Skipping output-folder file", key=key)
            continue

        ext = _extension(key)

        try:
            if ext in IMAGE_EXTENSIONS:
                _process_image(bucket, key)
            elif ext in TEXT_EXTENSIONS:
                _process_address_file(bucket, key)
            else:
                log("warning", "Unsupported file type — ignoring", key=key, ext=ext)

        except ClientError as e:
            log("error", "AWS ClientError", key=key,
                error_code=e.response["Error"]["Code"],
                error_message=e.response["Error"]["Message"])
            raise   


# ── Image processing ──────────────────────────────────────────────────────────

def _process_image(bucket: str, key: str) -> None:
    """
    Call Rekognition on the uploaded image.
    If a house label is detected above threshold, copy the object to Houses/.
    The original file is left in place so nothing is lost
    """

    log("info", "Running Rekognition label detection", key=key)

    response = rekognition.detect_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}},
        MaxLabels=20,
        MinConfidence=HOUSE_CONFIDENCE_THRESHOLD,
    )

    detected_labels = {label["Name"] for label in response.get("Labels", [])}
    house_matches   = detected_labels & HOUSE_LABELS

    if house_matches:
        dest_key = f"Houses/{_filename(key)}"
        _copy_object(bucket, key, bucket, dest_key)
        log("info", "House detected — copied to Houses/",
            source=key, destination=dest_key, matched_labels=list(house_matches))
    else:
        log("info", "No house labels detected — file left in place", key=key,
            detected_labels=list(detected_labels))


# ── Address Processing ───────────────────────────────────────────────────

def _process_address_file(bucket: str, key: str) -> None:

    log("info", "Downloading address file", key=key)

    obj      = s3.get_object(Bucket=bucket, Key=key)
    raw_text = obj["Body"].read().decode("utf-8", errors="replace")

    addresses = [line.strip() for line in raw_text.splitlines() if line.strip()]

    us_addresses            = []
    international_addresses = []

    for address in addresses:
        if _is_us_address(address):
            us_addresses.append(address)
        else:
            international_addresses.append(address)

    log("info", "Address classification complete",
        total=len(addresses), us=len(us_addresses),
        international=len(international_addresses))

    _write_text_to_s3(bucket, "Addresses/us_addresses.txt",            us_addresses)
    _write_text_to_s3(bucket, "Addresses/international_addresses.txt", international_addresses)


def _is_us_address(address: str) -> bool:
    address_upper = address.upper()

    if NON_US_COUNTRIES_RE.search(address_upper):
        return False

    has_us_country = bool(US_COUNTRY_RE.search(address_upper))
    if has_us_country:
        return True

    has_state = any(f" {s} " in f" {address_upper} " or address_upper.endswith(f" {s}") for s in US_STATES)
    has_zip   = bool(ZIP_CODE_RE.search(address_upper))

    if has_state and has_zip:
        return True
    
    if has_state:
        return True

    return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extension(key: str) -> str:
    """Return lowercase file extension including the dot, e.g. '.jpg'"""
    return "." + key.rsplit(".", 1)[-1].lower() if "." in key else ""


def _filename(key: str) -> str:
    """Return just the filename portion of an S3 key."""
    return key.split("/")[-1]


def _copy_object(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
    """Copy an S3 object; preserves server-side encryption."""
    s3.copy_object(
        CopySource={"Bucket": src_bucket, "Key": src_key},
        Bucket=dst_bucket,
        Key=dst_key,
        ServerSideEncryption="AES256",   # enforce encryption on destination
    )
    log("info", "Object copied", src=src_key, dst=dst_key)


def _write_text_to_s3(bucket: str, key: str, lines: list) -> None:
    """Write a list of strings to S3 as a newline-separated text file."""
    if not lines:
        log("info", "No lines to write — skipping", key=key)
        return

    content = "\n".join(lines) + "\n"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain",
        ServerSideEncryption="AES256",
    )
    log("info", "Wrote output file", key=key, line_count=len(lines))
