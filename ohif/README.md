# OHIF Configuration

This directory contains configuration for the OHIF Viewer when running via `docker compose`.

## File

- `app-config.js` — The main configuration file mounted into the `ohif/app` container.

## How it works

When you run `docker compose up`, the OHIF service mounts this file at:

```
/usr/share/nginx/html/app-config.js
```

This tells OHIF to use our local DICOMweb server (running on the host-mapped port 5985) as its data source.

## Editing the config

After changing `app-config.js`, restart the OHIF container:

```bash
docker compose up -d --force-recreate ohif
```

Or simply:

```bash
docker compose restart ohif
```

## Notes

- URLs in the config must be reachable **from the browser**, not from inside the Docker network.
- Our DICOMweb server already enables wide-open CORS (`*`), which is required for direct browser connections.
- For production use you would typically put a reverse proxy in front and adjust the roots accordingly.
