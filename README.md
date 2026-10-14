# Winter CAMs Viewer — GitHub Pages

A standalone winter-weather forecast website styled after the existing North Platte CAMs page (black/charcoal background, model tabs, control bar, forecast-hour tiles and a large centered map). **This is the WEBSITE only**; it does not change the existing severe-weather viewer, plot Python, AWS credentials or model automation. A four-panel feature can be built later.

## Website files

```text
winter-cams-viewer/
├── index.html             ← GitHub Pages homepage
├── .nojekyll
├── assets/
│   ├── config.js          ← public AWS S3 bucket URL and index prefix
│   ├── viewer.js          ← runs, products, domains, animation and PNG saving
│   └── site.css           ← visual styling matching the CAMs viewer
└── README.md
```

## Updated CAMs-style layout

This version follows the established CAMs visual style without HOME/WINTER CAMs breadcrumbs or subtitle: a black/charcoal header, **Winter CAMs Viewer** centered in the masthead, **Created by Matthew Labenz** in the upper-right, and a model-navigation row showing **HRRR / RRFS / RRFS-MPAS**. RRFS-MPAS is the only active model while the other winter model feeds are under development. Model, product, domain, run, animation, and AWS S3 controls remain connected to the winter manifest format. This version also enables **View → Compare Products** with two synchronized panels and combined PNG saving. GIF export and four-panel mode are intentionally deferred.

If replacing a previously published GitHub Pages site, upload the updated `index.html`, `assets/site.css`, and `assets/viewer.js` to the same repository (or upload this whole folder). Commit to `main`; GitHub Pages will republish automatically. A hard refresh (Ctrl+Shift+R) may be useful if the old stylesheet is cached.

## Publish to GitHub Pages

1. On GitHub, create a **new public repository** such as `winter-cams-viewer` (no need to make changes to your existing CAMs repository).
2. Upload **the contents of this folder** to the new repository root — not a wrapping `winter_cams_github_pages` directory. Preserve the `assets` subdirectory.
3. Commit the files to the `main` branch.
4. Open your repository **Settings → Pages**. Under **Build and deployment**, choose **Deploy from a branch**, select `main` and `/(root)`, and save.
5. After GitHub Pages completes its deployment, open `https://YOUR-GITHUB-USERNAME.github.io/winter-cams-viewer/` (the exact link is shown in the repository's Pages settings).

The website is deployable immediately. **Until the winter forecast pipeline publishes `runs.json` and map images to S3, the site intentionally shows an “Awaiting RRFS-MPAS winter graphics” placeholder** rather than inventing forecast graphics.

## Connecting existing AWS S3 graphics

The website's `assets/config.js` currently uses the SAME bucket URL embedded in an earlier version of your `cams.html`:

```js
window.WINTER_CAMS_CONFIG = Object.freeze({
  dataBaseUrl: 'https://mtl-nwslbf-model-data.s3.us-east-2.amazonaws.com',
  prefix: 'runs/cams/rrfs_mpas/winter/',
  animationMs: 650
});
```

Please confirm the bucket is still correct before going live. The viewer expects files from the automation pipeline in this form:

```text
runs/cams/rrfs_mpas/winter/
  runs.json
  2026101300/
    manifest_f006.json
    lbf/
      ptype_ref1km_f006.png
      qpf_f006.png
    regional/
      ptype_ref1km_f006.png
```

The `runs.json` format and per-hour manifest are the same ones produced by the existing RRFS-MPAS winter Python staging script and S3 publisher. Forecast hours that have not been published show unavailable forecast-hour tiles. Product selection adapts to the product PNGs listed in each manifest. Data arrives automatically as soon as it has been published; the website requires no redevelopment for new runs.

**S3 permissions:** `runs.json`, manifests and PNGs must be readable by site visitors using HTTPS. Either use appropriately restricted public-read content (if your bucket policy permits it) or use CloudFront with Origin Access Control and set `dataBaseUrl` to the public CloudFront URL. Do not store AWS credentials in any website file.

If GitHub Pages and S3 use different origins, configure CORS on the S3 bucket used for the files. A starting point for the AWS S3 **Permissions → CORS** JSON is:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedOrigins": ["https://YOUR-GITHUB-USERNAME.github.io"],
    "ExposeHeaders": [],
    "MaxAgeSeconds": 3600
  }
]
```

Replace the example GitHub username with your actual Pages origin. CORS is not a substitute for object read permissions. The **Save PNG** button needs CORS; if you cannot enable it, the **Open original PNG** link lets users open/save directly.

## Local preview

From the site folder run `python -m http.server 8000`, then visit `http://localhost:8000/`. If the S3 index is not available yet, you will see the styled placeholder and working website shell. Avoid opening with `file://`, which can trigger fetch restrictions.

## Existing features and next steps

* Same overall black CAMs visual style and familiar layout.
* Model-ready design: RRFS-MPAS first. HRRR and operational RRFS appear as non-interactive model labels until their winter feeds are ready.
* Run selection, three domains (LBF/Regional/Central Plains), a product dropdown, forecast-hour slider, blue available-hour tiles with 000-style numbers, animation, valid UTC time, keyboard arrow navigation and full-image/PNG saving.
* Linkable selections through the page URL (`run`, `fhr`, `product`, `domain`).
* The site does **not** embed AWS credentials, require server software or modify your current CAMs site.
* No four-panel UI yet, per your preference to focus on the new site first.

When this page looks right on GitHub Pages, connect the winter graphics pipeline, then consider HRRR/RRFS and a four-panel design.

## Latest `cams.html` reference

This edition is based on the full `cams.html` you shared on October 13, 2026, not just its screenshot: its top-toolbar layout, Single Panel / Compare Products selector, dark styling, blue forecast-hour tiles and panel sizing. **Compare Models** remains unavailable until HRRR/RRFS winter files are connected. Your original `cams.html` references `assets/gif.js`, which was not included in the HTML-only upload; the winter website intentionally omits an inactive Save GIF button until a complete, tested GIF export implementation is added. It does not require the `gif.js` file to deploy.

For comparison mode, both panels use the *same model, run, domain, forecast hour*, with independent left/right winter product selectors. The Save Two-Panel PNG button stitches both images side by side, using browser canvas and AWS CORS-enabled GET requests.
