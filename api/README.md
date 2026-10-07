# Realtime QA And Image API

Run with the LWA environment; no additional web framework is required:

```bash
source /fast/rtpipe/use_lwa.sh
python -m lwasolarproc.api_service --host 127.0.0.1 --port 8080 \
  --qa-db /fast/rtpipe/qa/qa.db \
  --image-db /fast/rtpipe/qa/images.db
```

After reinstalling the editable package, `lwasolarproc-api` is the equivalent
command. From the checkout, `python api/serve.py` also works. Use `--host 0.0.0.0`
to allow requests from other machines on the internal network. The service is
read-only, has no authentication, and defaults to localhost.

Enable the image producer by adding this argument to the realtime manager:

```text
--image-cache-db /fast/rtpipe/qa/images.db
```

Keep `--qa-db /fast/rtpipe/qa/qa.db` enabled. Both databases must be on local
disk. Restart the realtime service after adding the cache argument to its
`ExecStart`. QA schema migration happens when its writer opens the database.

## Requests

All timestamps are UTC `YYYYMMDDTHHMMSS`, optionally ending in `Z`. The
pipeline's `YYYYMMDD_HHMMSS` format is also accepted. Omitting `timestamp` or
passing `timestamp=newest` returns the latest observation, regardless of
worker completion order. Historical requests return the nearest observation;
ties select the newer observation. JSON includes the chosen `timestamp`,
`requested`, and `delta_seconds` for historical requests.

| Endpoint | Response |
| --- | --- |
| `/health` | Service availability and database file presence |
| `/flagging?timestamp=20261007T220000` | Nearest QA run, with per-band flagging rows |
| `/flux?timestamp=newest` | Latest QA run with available A-Team flux rows |
| `/images?timestamp=newest` | Four images' frequencies, observation dates, and download URLs |
| `/images/30.npz?timestamp=20261007T220000` | Nearest cached 30 MHz selection as compressed NPZ bytes |
| `/images/45.fits` | Latest 45 MHz selection as FITS bytes |

Image endpoints also accept `60` and `75`. QA timestamps are retained in the
existing database; images have a ten-minute window. `400` indicates a malformed
request, `404` means no eligible data/endpoint, and `503` means a database is
missing or inaccessible. `/health` checks file presence, not pipeline progress.

```bash
curl 'http://127.0.0.1:8080/flagging?timestamp=newest'
curl 'http://127.0.0.1:8080/flux?timestamp=20261007T220000'
curl 'http://127.0.0.1:8080/images'
```

Flagging rows contain `n_bad_ant`/`bad_ant` (count), `ant_list` (list), and
`flagged_frac`/`flagging_ratio` (fraction from 0 to 1). `ant_list` is the
zero-based `ANTENNA` table index, not a station name or physical stand number.
An antenna is bad only if all samples on all its participating baselines are
flagged. Antennas with no baselines are excluded. SQLite stores the list as JSON
text; the API returns a JSON array. Existing records have `ant_list: null`;
new records with no bad antennas have `ant_list: []`.

Flux rows include `source`, `freq_mhz`, `measured_jy`, `expected_jy`, `ratio`
(measured/expected), `beam`, `n_components`, `leakage_iv`, and `caltable`. The
existing QA measurement skips sources below 30 degrees elevation.

## Image Arrays

Workers choose the nearest available fine-channel Stokes-I plane to each of
30, 45, 60, and 75 MHz from the level-1 helioprojective products. `freq_mhz`
reports the actual channel center; the target need not be an exact channel.
These images already have the pipeline's beam and brightness-temperature
conversion. They do not apply an extra beam correction or refraction shift.

NPZ contains `image` (2D array), `freq_mhz`, `target_mhz`, `date_obs`, `bunit`,
and `header` (serialized FITS header, including spatial WCS and beam metadata).
All entries are loadable without pickle:

```python
import io
import numpy as np
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8080/images/60.npz") as response:
    payload = response.read()
with np.load(io.BytesIO(payload), allow_pickle=False) as arrays:
    image = arrays["image"]
    frequency_mhz = float(arrays["freq_mhz"])
```

FITS/NPZ bytes are stored in SQLite, so worker scratch cleanup does not invalidate
downloads. Each worker write expires observations older than ten minutes
relative to current UTC and inserts four planes in one transaction. Expired
images are also excluded at query time if no worker has written recently.
Observations already older than ten minutes when finished are not cached.
WAL mode allows concurrent API readers. A local `images.db.lock` file serializes
worker initialization and writes; SQLite also uses a 30-second busy timeout.

## User Service

The supplied `lwasolarproc-api.service` is independent of the realtime worker
service. Install it without sudo:

```bash
mkdir -p ~/.config/systemd/user
cp /fast/rtpipe/lwasolarproc/api/lwasolarproc-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lwasolarproc-api
systemctl --user status lwasolarproc-api
```

The unit defaults to localhost port 8080. Edit its paths or bind address as
needed. The already enabled user lingering keeps it running after logout.

## Verification

```bash
source /fast/rtpipe/use_lwa.sh
python -m pytest /fast/rtpipe/lwasolarproc/tests/test_api.py -q
```
