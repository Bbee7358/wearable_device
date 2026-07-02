# Netlify + Web Serial API version

This branch adds a static Web Serial API build under `public/`.

## Local preview

```sh
cd public
python3 -m http.server 8000
```

Open `http://127.0.0.1:8000/` in Chrome or Edge.

## Netlify settings

- Build command: leave empty
- Publish directory: `public`

Netlify also reads `netlify.toml`, so importing the GitHub repository is enough in most cases.

## Browser requirement

Web Serial API works only in supported desktop browsers such as Chrome or Edge and requires a secure context. Netlify provides HTTPS automatically.
