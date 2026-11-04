# Framekeep portfolio guide

Suggested repository name: **framekeep**

Suggested description:

> A private Windows photo organizer with metadata timelines, on-device face grouping, exact duplicate detection, and verified copy-only exports. Python, FastAPI, SQLite, OpenCV, and vanilla JavaScript.

Suggested topics: python, fastapi, sqlite, opencv, photo-organizer, computer-vision, privacy, windows, portfolio.

## Recruiter introduction

> Framekeep turns a personal problem—sorting years of old phone backups—into a local photo management application. It combines metadata extraction, streaming file hashing, a persistent SQLite index, and on-device face similarity with a Windows desktop interface. Its copy workflow previews the folder structure, handles filename collisions, and verifies each result without modifying the original collection.

Describe your role accurately. This project was created with AI coding assistance. Explain the problem, product decisions, testing you performed, and improvements you personally made. Avoid claiming that you independently wrote or trained every part. Its face models are pretrained OpenCV models.

## Two-minute demo

1. Open Framekeep and choose **Explore a sample library**, then browse its automatic Albums inside the app.
2. Show the monthly timeline, then filter to PNG, Screenshots or Illustrations. Demonstrate a manual category correction. For visual detection, use a permitted test collection and the optional local model; explain Needs review.
3. Open a landscape, add a tag, and favorite it.
4. Open **Needs review**, run **Analyze photos** on permitted sample images, and confirm an uncertain AI category. In **Duplicates**, compare the Exact duplicates and Similar photos tabs and open a side-by-side comparison.
5. Preview a Category → year → month export to a disposable local output folder. Show Photos/Screenshot/Illustration/Video folders, collision handling, verified copies and the manifest.
6. For People, use a separate test collection whose subjects have consented. Demonstrate naming, merging, and separating suggestions.
7. Use a permitted iPhone Live Photo or the generated motion test. Show that its still and motion count as one memory, play its motion, and preview a paired export that retains both originals.

The bundled illustrations contain no faces. They do not demonstrate recognition accuracy.

## Decisions worth discussing

- A Windows WebView2 host provides a desktop window and icon while reusing the local HTML/CSS/JavaScript interface and authenticated loopback backend. Browser mode remains available for development.
- Desktop lifecycle separates a newly owned service from an already-running service; a per-launch ownership marker survives Windows Python redirector PIDs. Single-instance events reactivate the existing window, and private WebView profiles are kept separate from normal browsers.
- EXIF → filename → modified-date precedence makes date uncertainty visible.
- SHA-256 distinguishes exact copies without discarding near-duplicate memories.
- Perceptual pHash and dHash, aspect ratio and average color offer conservative visual-copy candidates. A bounded BK-tree search returns direct pairs, with visible analysis coverage and search limits. These suggestions never change the copy-only deduplication rule.
- Incremental indexing and paginated results make repeat scans and browsing manageable.
- One background worker avoids competing disk-heavy jobs while the UI stays responsive.
- Unique export directories, exclusive creation, and hash verification simplify recovery.
- Local inference removes a hosted recognition dependency; manual corrections acknowledge uncertainty.
- Automatic albums derive category/year/month groups from the index, so browsing needs no physical file reorganization. The gallery paginates results and the image viewer continues across page boundaries.
- The interface uses a photo-first hierarchy, two grid densities, a contextual selection toolbar and self-hosted typography. Its [design notes](DESIGN.md) and [editable Figma companion](https://www.figma.com/design/a1Hj30Ogrd9oVQlxw3nW39?node-id=10-5) document the decisions using generated sample artwork only.
- Live Photo pairing follows shared Apple identifiers rather than guessing from names or timestamps. Logical memories and physical resources are counted separately. On-demand bounded previews provide browser compatibility, while verified exports keep both original resources together.

## Before publishing

- Run the checks in the README.
- Keep backups and organized outputs outside the source checkout.
- Review every file selected for the first commit.
- Include application code, tests, documentation, license notices, and the sample-only screenshot.
- Exclude virtual environments, SQLite databases, thumbnails, face crops/vectors, ONNX weights, logs, manifests, and private photos.
- Use only sample illustrations or images you have permission to publish in screenshots.

No GitHub repository is created or pushed by the setup scripts. GitHub Pages cannot run the Python backend or access a visitor's local folders. Recruiters can inspect the source, follow setup instructions, and view the sample screenshot. A downloadable desktop release can be added later.

## Future work

- Standalone Windows executable and installer; the current desktop launch still uses the checkout and Python virtual environment.
- Broader near-duplicate evaluation for crops, burst photos and color edits.
- Date correction and timezone handling.
- Large-library benchmarks and more varied face evaluation.
- Broader video container support, progressive playback for longer videos, custom thumbnail selection and HDR tone mapping.
- Automated browser regression tests and accessibility improvements.
