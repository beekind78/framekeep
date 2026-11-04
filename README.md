<p align="center"><img src="framekeep/static/icon.svg" width="64" alt="Framekeep"></p>
<h1 align="center">Framekeep</h1>
<p align="center">A local photo library for Windows. Browse phone backups without moving the originals.</p>

[![Tests](https://github.com/beekind78/framekeep/actions/workflows/ci.yml/badge.svg)](https://github.com/beekind78/framekeep/actions/workflows/ci.yml)

![Framekeep gallery with original sample illustrations](docs/screenshots/gallery.jpg)

Phone backups can mix camera photos, screenshots, saved artwork and Live Photo video clips in the same folder. Framekeep groups them into albums you can browse inside the app. Originals stay in place; creating organized copies is optional.

Read the [project case study](PORTFOLIO.md) for the background, implementation choices and current limits, or follow the [Windows setup](#run-on-windows) to try it with the sample library.

## Features

- Browse albums and a monthly timeline, with filters for category, date, format, camera, filename and tags. Choose a comfortable or compact grid, save favorites and correct categories in place.
- Index JPEG, PNG, HEIC/HEIF, WebP, GIF, TIFF, BMP and AVIF images, plus MOV/MP4 videos. Media checks use file contents to handle incorrect backup extensions.
- View an iPhone Live Photo as one photo, with its motion clip available in the viewer. Pairing uses embedded Apple identifiers; uncertain matches stay separate.
- See real video frames in thumbnails and click the play icon to watch inside the app. A separately installed FFmpeg prepares local previews with sound, seek, speed and fullscreen controls.
- Copy selected originals to the Windows clipboard, edit tags, or rotate and crop a new image copy. Removing photos or folders from Framekeep leaves their files on disk and allows restoration from the sidebar.
- Find exact duplicates with SHA-256, and review possible resized or recompressed copies side by side. Optional local models suggest photo categories and groups of people; their results can be corrected manually.
- Preview an export by category → year → month, date, format or people. Copies use collision-safe filenames, hash verification and a local CSV manifest. Live Photo exports keep both original resources together.
- Explore generated sample landscapes before adding a personal folder. The app needs no cloud account or API key.

## Run on Windows

Requires **64-bit Python 3.11 or later** on PATH and Microsoft Edge WebView2 Runtime for the desktop window. Current Windows installations often already include WebView2. The host uses pywebview with the Edge Chromium renderer; it does not fall back to the old Internet Explorer renderer. Python's normal Windows installer includes Tk, used for the folder picker. A typed folder path also works.

1. Clone or download this repository.
2. Double-click **Setup Framekeep.cmd** once to create a virtual environment and install dependencies.
3. Open the **Framekeep desktop icon** or **Start Framekeep.cmd**. Setup creates Desktop and Start menu shortcuts; **Create Framekeep shortcuts.cmd** can recreate them after moving the checkout.
4. Framekeep opens in its own Windows window. Choose **Add folder**, or explore the sample library. Reopening the shortcut brings the existing window forward. No browser tab is opened.

Close the Framekeep window to stop the local service it started. If a task is active, the app asks whether to stop it safely; completed copies remain. **Stop Framekeep.cmd** also closes the desktop window. If desktop mode attaches to an independently started browser/development service, closing its window leaves that shared service running. Run Stop Framekeep.cmd again after the window closes to stop that independent service.

For development:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe run.py
~~~

Add --no-browser to run without opening a browser. The app binds only to the loopback interface. It is intended for one user on one PC, and is not a server for public hosting.

For desktop development, run `.\.venv\Scripts\python.exe desktop.py`. The normal shortcut uses pythonw.exe, so no terminal appears. Browser mode remains available through run.py for development; a bare browser URL stays locked until a one-use launch ticket authorizes the tab.

## Typical workflow

1. Connect a phone backup folder once. The scan creates an index and thumbnails; it does not move source files.
2. Start in **Albums**, or choose a source on the left to see its albums. Open Photos, Screenshots or Illustrations to browse the collection immediately. Year and month buttons narrow the view when useful.
3. Open a picture and use the on-screen arrows or left/right keys. The item counter shows your position in the matching collection; the next page loads automatically at the boundary.
4. Correct a category with **Save category**, or select several pictures and choose **Set category**. Album membership updates immediately and your choice is kept for unchanged files.
5. For optional face grouping, open **People → Set up people** to download approximately 39 MB of pinned, checksum-verified models from OpenCV Zoo. Review, name, merge and separate suggestions.
6. If you also want organized copies on disk, choose items or filters, then **Organize copies**. Folder export is optional; it is not required to view albums.

For visual categories, choose **Detect categories** above the gallery. The first run downloads an approximately 85 MB pinned, checksum-verified CLIP model; subsequent detection runs locally without Internet access. Camera make/model metadata, explicit screenshot filenames, and animation metadata take precedence over visual guesses. Other images use the local visual model, with close matches left in Needs review. Existing images need this detection step once. New images are categorized during indexing after the model is installed. Use the Photos, Screenshots, Illustrations and Needs review buttons to filter the current collection. Open an image and choose **Save category**, or select several images and choose **Set category**, to correct suggestions. **Use automatic suggestion** clears a manual override. **By category** and **Date & category** are available in Organize copies.

Live Photos are included in **Albums → Photos**, alongside still pictures. The Photos album shows how many Live Photos it includes; **All photos** is the default and **Live Photos only** is an optional filter inside that album. The separate Live Photos sidebar page has been removed. The Videos album excludes linked motion clips by default, including API album reads. Manual category corrections remain in place. Choose **Photos → Find Live Photos** once for an existing index; new scans read pairing metadata automatically. Open a Live Photo and choose **Play Live Photo**; **Show still photo** returns to its image. Playback uses a cached local H.264/AAC MP4 preview, requires FFmpeg on PATH, and plays muted until you enable audio. Counts show one memory per linked pair while both files remain in the index. Exporting either resource automatically includes both originals in the still photo's target folder, with a shared filename stem. Deduplication skips an identical complete pair, preserving distinct motion even when two still images are byte-identical. Original Apple metadata is retained in verified copies. This supports browsing and preservation, not Apple's editing, effects or iCloud synchronization.

Choose **Needs review → Analyze photos** to recheck uncertain categories and build visual fingerprints for the current source/search/date scope. The model download is shared with Detect categories. Existing manual choices remain in place; analysis skips video decoding. A best suggestion is still uncertain until you confirm it. Open a picture to see its exact copies and possible visual matches, then compare them side by side. **Duplicates → Similar photos** displays direct matching pairs, not transitive groups. The coverage count shows how many images have fingerprints. Very plain images are excluded, and a search-limit notice asks you to narrow the filters if needed. This is a conservative resized/recompressed-copy check; crops, bursts, color edits and other visually related images can be missed. No match percentage is presented as an accuracy estimate.

Choose **Organize copies → Category → year → month** to create the following structure directly in your chosen output folder. The four main folder names match Photos, Screenshot, Illustration and Video; only years/months containing copies are created. Needs review and Animated get their own folders when present. Choose All media and clear selection/filters to include all categories from a source. Keep **Put this run in a new Framekeep folder** unchecked for direct output; check it to keep a separate run directory.

Example output:

~~~text
Your output folder/
├── Photos/2026/April/IMG_0001.heic
├── Screenshot/2026/April/screen.png
├── Illustration/2025/January/drawing.jpg
├── Video/2026/April/clip.mov
└── Framekeep-manifest-20261105-001500-a1b2c3.csv
~~~

Direct output reuses existing folders without overwriting files; conflicting names receive a numbered suffix. Each direct run gets a uniquely named CSV manifest. With the optional run directory, the manifest is named manifest.csv. Changes to a source after indexing cause that file to be skipped with an error. Cancellation keeps completed copies and their manifest entries. Output links/junctions are rejected, and Windows directory handles prevent swapping destination folders during copying.

Photo formats are confirmed by the image decoder, and videos require a video track in their MOV/MP4 metadata. If a filename extension is wrong, organized copies receive the detected extension (for example, JPEG data named `photo.MOV` becomes `photo.jpg`, while a QuickTime movie named `clip.HEIC` becomes `clip.mov`). Source names and contents stay unchanged. Scan reports count photos, videos and skipped metadata separately; video files are never decoded as photos.

## Selection and editing

Ctrl+click toggles a card, Shift+click selects a range in the displayed order, and Ctrl+Shift adds a range. Checkboxes also work. Ctrl+A selects all currently displayed items; load more results first to include them. Ctrl+C copies selected originals to the Windows clipboard. Delete opens a confirmation to remove them from Framekeep, leaving the files on disk. These shortcuts do not intercept typing fields, dialogs, the viewer or open menus.

Right-click a selected card to act on the whole selection. Right-click an unselected card to select it alone. The menu offers copying files or paths, verified copies to another folder, and removal from the catalog. It also provides tags, categories, favorites, image editing and Show original in folder for individual items. Shift+F10 opens the menu from the keyboard. Source-folder menus include open, rescan, reveal and Remove folder from Framekeep.

Batch actions accept up to 5,000 items and include linked Live Photo resources once. Stale or unavailable selections are rejected without partially removing other items. **Removed items** in the sidebar restores photos or folders and rescans their source. Tags, categories, favorites and people matches are retained, and ordinary rescans keep individual removals hidden.

**Edit image copy** provides rotation and a draggable crop, then saves a new JPEG in a separate folder. Saving uses the full-resolution source and verifies the output. Editing a Live Photo or animation creates a still image; its original resources remain intact.

## Privacy and local data

Framekeep does not upload photos, run analytics or send inference requests to a cloud service. Fonts and interface assets are bundled locally, and the app needs no login. Internet access is needed for package installation and optional model downloads. ONNX Runtime telemetry is disabled in the category worker.

The default data location is **%USERPROFILE%\.framekeep\data**. This profile location is shared by ordinary Windows launches and processes started from packaged desktop apps, avoiding AppData virtualization. It contains the SQLite index, thumbnails, face previews/vectors, model weights, and optional demo illustrations. Set the FRAMEKEEP_DATA environment variable before starting to use another location. Versions before 1.1.1 used AppData; keep that directory until existing library data has been migrated.

Export manifests contain source file paths and belong with your private collection. Keep actual photo backups outside this repository. Personal media formats, catalogs, credentials and local diagnostics are ignored. Before publishing, run `python scripts/check_publication.py --staged` to inspect the actual Git snapshot against the source allowlist and the hashes of reviewed demo assets. See [publishing safely](docs/PUBLISHING.md). These checks complement a manual review; they cannot identify every possible private value in arbitrary source text.

Every sensitive API read and mutation requires a random session credential. The launcher stores a separate per-process control credential in a private runtime directory; on Windows the directory/file DACL permits only the owner and SYSTEM, and DPAPI encrypts the credential for the current Windows user. A random HMAC challenge proves the listener's identity before the launcher sends control authority. Fresh server instances rotate credentials.

The launcher supplies a one-use ticket that expires after 60 seconds. The browser removes it from the URL fragment before exchanging it for a 12-hour session. The session token stays in origin-and-port-scoped sessionStorage and is attached to API headers, including authenticated image fetches. No localhost authentication cookie or credential-bearing image URL is used. Other local Windows users cannot obtain authority merely by calling the API. Open the launcher again after restarting the server or when a session expires.

The desktop host uses the same ticket and header authentication. WebView2 runs in private mode with a separate temporary profile under the protected runtime directory; it does not reuse your Edge/Comet browser profile. It exposes no application Python API to page JavaScript. Top-level and frame navigation are restricted to the exact local app root; downloads, file URLs, browser permissions, external popups and developer tools are disabled. A per-launch marker in the DPAPI-protected runtime record distinguishes an app-owned service from a borrowed one, including when Windows Python redirectors change process IDs. Named events only activate or close the per-user window; they carry no photo data or session credential.

Origin/fetch-site checks, a restrictive content security policy, trusted-host checks, and same-origin resource protection remain enabled. Runtime credentials live under **%USERPROFILE%\.framekeep\runtime** independently of FRAMEKEEP_DATA; launcher logs use **%USERPROFILE%\.framekeep\logs**. Do not publish or share runtime credentials. The application does not encrypt photo thumbnails, face vectors, metadata or export files at rest, and cannot protect against malware already running as your Windows account or an administrator.

Original-image parsing and face extraction run in a disposable process with a **25-second wall timeout**, **20-second CPU budget**, **1 GiB process-memory cap**, and one concurrent image worker. Windows uses Job Object limits; Linux uses resource limits. If limits cannot be installed, decoding fails closed. This isolates failures and resource use, but is **not an OS privilege sandbox**: the worker still runs as your account.

Only JPEG, PNG, WebP, GIF, TIFF, BMP, HEIF and AVIF decoders are permitted. Each original must be at most **256 MiB encoded**, **50 megapixels**, and **30,000 pixels on either side**. EXIF rotation and original date precedence are preserved; preview JPEGs omit source EXIF. Oversized, unsupported, corrupt and timed-out files are reported as skipped. Existing indexes are revalidated under this policy on the next rescan.

MOV/MP4 metadata runs in the same bounded worker, using a Python atom reader with a **1 MiB metadata-read budget**, **4,096 atom limit**, bounded nesting and a **64 GiB video file limit**. It seeks past video payloads, never decodes frames, and never follows container references. Movie creation timestamps are interpreted as UTC and converted to the PC's local time; absent timestamps fall back to filename/file-modified dates. Original movie files are copied and hash-verified without conversion.

Live Photo motion playback is prepared only after an authenticated Play request. A separate worker constrains its FFmpeg child to local MOV input, with network protocols and external track references disabled. It verifies the input SHA-256 before decoding and strips metadata from the preview. Limits are 128 MiB input, a 10-second source, a preview of up to 6 seconds/960 pixels/24 fps/8 MiB, an 18-second FFmpeg timeout, and the existing 25-second worker wall limit, 20-second process CPU cap and 1 GiB per-process memory cap. Its Windows job allows the wrapper plus one decoder child and kills both if the wrapper exits. Image workers retain their one-process limit. Preview MP4s remain in local application data. HDR previews may differ in appearance; original HDR resources remain unchanged.

Video thumbnails use authenticated image requests and an independent JPEG cache targeting 64 MiB. The UI requests at most two video posters concurrently and defers off-screen posters. The source is checked against its indexed size, timestamp and SHA-256, copied read-only into a parent-owned temporary directory, then decoded with local MOV input and external references disabled. A representative frame is selected from 12 downscaled frames near the start, with source orientation applied; JPEG output is limited to 480 pixels per side / 512 KiB and stripped of source metadata. Native work shares the image-worker semaphore and has an 18-second FFmpeg timeout, 25-second worker wall limit, 20-second process CPU cap, 1 GiB per-process memory cap and a two-process Windows job. The parent cleans temporary source copies even after worker failure or timeout. Thumbnail generation supports source files up to 2 GiB and does not require converting the full movie. Cache hits recheck source availability and size/timestamp; reindexing a changed source selects a new hash-based entry. Originals and library records are never changed by browsing.

Ordinary video playback uses a separate cancellable worker, one conversion at a time, with the same local-only demuxer policy and SHA-256 verification. Full movies are prepared up to 30 minutes / 2 GiB input, 720p / 30 fps output, 256 MiB output, 180 seconds wall time, 150 seconds CPU per process and 1 GiB memory per process. The wrapper and FFmpeg share a kill-on-close Windows job limited to two processes. Closing the viewer or app cancels unfinished preparation and removes temporary files. Failed, oversized and incomplete outputs are rejected rather than shown as truncated videos. Generated MP4s are served only through the authenticated API, fetched with a header credential and played from a temporary blob URL; no credentials appear in media URLs. The managed playback cache targets 512 MiB, excluding a current temporary conversion or files still held open by a response. Originals are never converted in place. Playback is optimized for browsing; HDR colour may differ and professional editing/subtitles/multiple audio tracks are not included.

Descendant links, Windows junctions and reparse points are rejected. Original-file reads for decoding, hashing and export recheck the source boundary, including the physical opened-file path. For now, copy cloud placeholder files into an ordinary local backup folder before importing them. Choose local, nonsynchronized source/data/output folders when you want all copies to stay on this PC.

Context actions require the same session header and origin checks as other private APIs. Clipboard actions are explicit, write-only Windows operations over registered, checked source paths. File copies use Unicode CF_HDROP plus the copy drop effect, following the [Windows clipboard formats](https://learn.microsoft.com/en-us/windows/win32/shell/clipboard); no clipboard read permission or general native bridge is exposed. Copy to folder remains the hash-verified export workflow. Removing catalog entries invalidates pending export plans. Removed originals and their faces cannot be read through media APIs until restored.

Image editing keeps the existing 256 MiB input, 50 MP, 30,000-pixel side, 25-second wall, 20-second CPU and 1 GiB memory limits. It verifies the original SHA-256 before decoding, applies EXIF orientation before rotation/crop, and limits the saved JPEG to 10 MiB. It retains a capture timestamp and normalized orientation, omits source GPS and other EXIF, and uses a numbered suffix when the output name exists. Animation produces a still frame; video editing, RAW processing and color-managed HDR editing are not included. The preview is downscaled for display, while the saved crop uses the source resolution.

## Architecture

~~~mermaid
flowchart LR
    UI["Windows WebView2 window · HTML / CSS / JavaScript"] --> API["FastAPI · 127.0.0.1"]
    API --> JOBS["Single background worker"]
    API --> DB[("Local SQLite index")]
    JOBS --> PIL["Pillow + pillow-heif"]
    JOBS --> CV["OpenCV YuNet + SFace"]
    JOBS --> COPY["Copy + SHA-256 verification"]
    PIL --> SOURCE["Local photo folders"]
    CV --> SOURCE
    COPY --> OUTPUT["New output folder + manifest"]
~~~

The frontend uses browser APIs and local SVG icons, with no build step. The Python backend separates indexing/querying, background jobs, face analysis, and HTTP handlers. SQLite uses WAL mode with one connection per operation. Scans are incremental by file size and modification time, hashing only new or changed files. Gallery results are paginated.

People grouping compares normalized SFace vectors using cosine similarity. A conservative 0.48 threshold and at most eight reference vectors per group favor separate suggestions over premature merges. Manual corrections are stored locally. A changed file is reanalyzed on the next People run.

## Tests

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
node --check framekeep/static/app.js
~~~

Tests cover metadata/date precedence, corrupt files, HEIC/AVIF, EXIF orientation, duplicates, incremental policy revalidation, favorites/tags, verified exports, source changes, people corrections, every sensitive API read, one-use/expired tickets, session rotation, spoofed listeners, origin checks, decoder formats/limits, timeout and memory enforcement, and junction/path replacement. GitHub Actions runs the Python checks on Windows.

Category tests cover old-index migration, uncertain predictions, corrupted model downloads, authenticated category edits, manual override persistence/reset, category filters/counts, and verified category exports. Optional offline real-model tests use `FRAMEKEEP_CATEGORY_TEST_ASSETS` pointing to a directory containing `runtime/clip-vit-base-patch32-int8.onnx` and permitted validation images under `validation/`. They are skipped in CI rather than downloading weights during tests. The local smoke test distinguishes a camera image, an application screenshot, and an original sample illustration; this is a functional check, not a general accuracy benchmark.

Album checks cover authenticated reads, source/category/year/month/search/favorite filters, immediate regrouping after manual corrections, bounded covers, pagination, and unchanged source files and index records while browsing. Albums are derived from existing metadata and require no export folders or additional image analysis.

Review checks cover resized/recompressed copies versus exact copies, plain and unrelated images, independent direct pairs, search limits, authenticated/foreign-origin access, cached fingerprint invalidation, source changes, cancellation, manual category preservation, and video exclusion. Fingerprints are generated only inside the existing bounded image worker and are tied to the indexed SHA-256; stale fingerprints do not participate in matching.

Live Photo checks cover HEIC/JPEG Apple MakerNote identifiers, MOV metadata layouts/order, renamed resources, ambiguity, missing or replaced resources, collapsed counts, authenticated motion reads, cache/source validation, and hash-verified paired exports in several layouts. An optional offline FFmpeg integration check creates its own test clip and exercises conversion in the bounded worker.

Desktop checks cover native navigation restrictions, one-window activation events, real backend ownership across Python redirectors, unauthenticated API rejection, leaving a borrowed service running, clean close/restart, ticket rotation and unchanged original/category/tag/favorite data. Native UI verification uses generated sample artwork and motion clips.

The face pipeline was also smoke-tested with an OpenCV sample face and a rotated variant: two detections, one suggested person group. This is a functional check, not a measurement of recognition accuracy. No sample face is committed to this repository.

## Current limits

- Ordinary MOV/MP4 videos support local frame thumbnails, in-app playback within the limits above, metadata browsing and verified copying. First thumbnail/playback waits for local preparation; unsupported or over-limit files can be opened in another player through Show original in folder. A thumbnail is a sampled frame near the start and may not capture the most recognizable moment. Linked Live Photos have separate short motion playback. Camera RAW and other video containers are not indexed.
- Categories are suggestions. Screenshots containing photos or art, scanned documents, game captures, and stylized photography can be ambiguous. No photo is declared a screenshot merely because it has a PNG extension or phone-sized dimensions. Use Needs review and manual category corrections.
- Exact duplicate detection does not identify recompressed, resized, or visually similar photos.
- File-modified dates may reflect a backup date. EXIF dates are displayed as recorded and are not timezone-normalized.
- Face grouping needs review. It may miss faces or confuse people. It does not identify a person by name or infer personal attributes.
- Re-scans detect changes by size and modification time; content changed while preserving both may require re-indexing. Exports independently verify the source hash.
- Sources on a disconnected drive stay indexed until a successful rescan. Opening or copying originals requires reconnecting the drive.
- Large collections require time and disk space for thumbnails, hashing, and CPU inference. Very large libraries have not been benchmarked.
- Worker limits intentionally reject very large photos; process startup adds scanning overhead. Up to 50 detected faces are extracted per image.
- The Windows launchers and folder picker are the supported desktop workflow. There is no signed installer or standalone executable in this release.

## Project background and license

The [project case study](PORTFOLIO.md) describes the problem, implementation choices, validation and remaining work. [Interface notes](DESIGN.md) cover the gallery layout and selection behavior.

Framekeep was developed with AI coding assistance and uses pretrained recognition models.

Application code and original demo illustrations: [MIT](LICENSE). Model weights have separate licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
