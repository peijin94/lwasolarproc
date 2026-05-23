# lwasolarproc Container

This container builds `lwasolarproc` on top of the `astronrd/linc` Docker image. The base image provides the LINC radio-astronomy environment; this layer adds `lwa-solar-util` and the local `lwasolarproc` package.

Build from the `lwasolarproc` repository root so the Docker build context contains the package source:

```bash
cd /fast/rtpipe/lwasolarproc
docker build -f container/Dockerfile -t lwasolarproc:linc .
```

By default the build installs `lwa-solar-util` from `https://github.com/ovro-eovsa/lwa-solar-util.git` at `main`. To pin a branch, tag, or commit:

```bash
docker build \
  -f container/Dockerfile \
  --build-arg LWA_SOLAR_UTIL_REF=<branch-tag-or-commit> \
  -t lwasolarproc:linc .
```

Check the installed command-line entry points:

```bash
docker run --rm lwasolarproc:linc 'lwasolarproc-fullband --help'
docker run --rm lwasolarproc:linc 'lwasolarproc-realtime --help'
```

Run a full-band job by mounting a prepared directory that contains all band Measurement Sets for one timestamp, plus caltables and an output directory. Keep the working directory on a writable mount with enough space for Measurement Set copies and WSClean products:

```bash
docker run --rm \
  -v /fast/rtpipe/example_fullband_ms:/data/ms:ro \
  -v /fast/rtpipe/caltab_h5parm:/data/caltab_h5parm:ro \
  -v /fast/rtpipe/container_runs:/work \
  lwasolarproc:linc \
  'lwasolarproc-fullband \
    --ms-dir /data/ms \
    --caltable-dir /data/caltab_h5parm \
    --work-dir /work/fullband_20260519_212931'
```

For the structured slow-data tree at `/lustre/pipeline/slow/BAND/YYYY-MM-DD/HH/`, use `lwasolarproc-realtime --mode backlog` for fixed timestamp ranges instead of calling `lwasolarproc-fullband` directly.

Run the realtime manager against mounted slow-data and output trees:

```bash
docker run --rm \
  -v /lustre/pipeline/slow:/lustre/pipeline/slow:ro \
  -v /lustre/solarpipe/realtime_pipeline:/lustre/solarpipe/realtime_pipeline \
  -v /fast/rtpipe/caltab_h5parm:/fast/rtpipe/caltab_h5parm:ro \
  -v /dev/shm/tmp_pipe_dir:/dev/shm/tmp_pipe_dir \
  lwasolarproc:linc \
  'lwasolarproc-realtime \
    --mode realtime \
    --slow-root /lustre/pipeline/slow \
    --caltable-dir /fast/rtpipe/caltab_h5parm \
    --proc-tmp /dev/shm/tmp_pipe_dir/lwasunproc/proc_tmp \
    --ingest-lustre \
    --el-valid 13.5 \
    --workers 8 \
    --scan-interval 12 \
    --fch-pols I \
    --threads 16 \
    --do-refraction \
    --no-logging'
```

Notes:

- The Dockerfile uses `astronrd/linc:latest`. Pin the base image in production if repeatability matters.
- Build with `docker build -f container/Dockerfile ... .`; using `container/` as the build context will fail because the package source is outside that directory.
- The image sets writable cache/config paths under `/tmp` so SunPy and Matplotlib do not write into a read-only home directory.
- If Docker runs rootless or with restricted shared-memory defaults, mount a real scratch directory instead of `/dev/shm/tmp_pipe_dir`.
