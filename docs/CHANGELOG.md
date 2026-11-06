# Framekeep changelog

## 1.14.0

### Long tasks and People

- **Pause and resume after a restart.** Folder imports, rescans, category detection, photo analysis and Find people save their progress on this PC. Choose **Pause** before closing Framekeep, then choose **Resume** when you reopen it, even after shutting down Windows. If the app closes while processing a file, that file may be retried. Reconnect an external drive before resuming a task that uses it.
- **See how long a task might take.** The progress panel shows estimated time remaining once Framekeep has measured enough work. The estimate adjusts as processing speed changes and recalculates when the task resumes.
- **Scan People in smaller sections.** Choose a source folder, year, month or **Photos album only**. Scan the next 250, 1,000 or 5,000 pending photos, or scan all pending photos in that section. New runs start with the newest pending photos and skip unchanged files that already have face results.
- **Reduce repeated face processing.** Framekeep reuses face results for verified exact copies during a run, loads the recognition model when it finds a face, and compares faces with existing groups together. Detection and grouping run locally. You can review, name, merge or separate suggested groups.

### Categories and bulk review

- **New model for Photos, Screenshots and Illustrations.** SigLIP 2 replaces the previous visual model and uses the full image to consider clues such as status bars, chat layouts and illustration styles. Camera and animation metadata and explicit screenshot filenames still take precedence. Close calls stay in **Needs review**. The first use downloads a verified model of about 95 MB. Analysis then runs on your PC.
- **Separate pending photos from uncertain suggestions.** In **Needs review > Review in batches**, see which photos still need analysis and which already have a suggestion. Analyze the pending photos or refresh suggestions made by an earlier model. Framekeep preserves categories you set manually and can reuse predictions for verified exact copies.
- **Approve a group from its previews.** Pick a suggested category and review a grid of photos. Exact copies in the current view share one preview. Deselect any preview that belongs elsewhere, then approve the rest together.
- **Accept all suggestions across the current filters.** The confirmation shows how many photos will move to each category, including results beyond the page you loaded. It also shows how many suggestions come from an earlier model. Photos without a suggestion stay in **Needs review**.
- **Undo a batch later.** Open **Batch history** to reverse a bulk approval. Framekeep keeps manual category changes you made after that approval.
- **Group saved images in Downloads.** Choose **Downloads** in the category picker for one image or a selection. The album is available in the gallery and in category-based exports. You assign it manually.

### Image recovery and format fixes

- **Open multi-image MPO files.** Some backups give MPO files a `.JPG` name. Framekeep shows the first image in the gallery and preserves the complete MPO when making organized copies.
- **See clearer messages for incomplete images.** If the JPEG decoder detects missing image data, the scan report describes it as incomplete. Choose **Recover JPEG copies** to save the readable portion as a separate JPEG.
- **Compare a damaged image with a possible larger copy.** From the recovery report, choose **Review larger library matches** to see them side by side. Choosing **Approve larger match** or **Use recovered JPEG** moves the review to the next undecided image. Once you finish reviewing, **Create recovered copies** saves the results and verifies any approved matches.
- **Save and browse recovered copies in Framekeep.** Each run gets its own subfolder under **Framekeep Recovered** by default. You can also choose a destination. **View recovered photos** opens the new source directly so you can browse, tag and manage the saved copies.
- **Enlarge small recovered JPEGs.** The optional **AI enlargement (3×)** uses a small local model to enlarge the readable image. You can also trim a small gray edge before saving. A complete copy of the photo is needed to recover the original detail in missing areas.
- **Inspect a JPEG you already recovered.** Open **Repair photo** for an indexed JPEG to look for a larger library match or preview a crop that removes a large, flat gray band. Review the available result before saving it. **View repaired photo** opens the new copy in Framekeep.

### Video

- **Choose playback quality up to 4K.** The player defaults to 720p for quicker preparation. Select **High (up to 4K)** to keep more detail when the source has it. Each quality has a separate local preview, and the source video stays untouched. High-quality preparation can take longer and is subject to the same time and file-size limits.
- **Use an app-specific FFmpeg on Windows.** Framekeep checks its own tools folder first, then looks for FFmpeg on PATH. The source release uses the FFmpeg installation available on your PC.

### Browsing and controls

- **Keep your place after opening a photo.** Closing the viewer returns you to the same gallery position with the pages you already loaded. Thumbnails load near the visible part of the gallery and release their image resources as you scroll away.
- **Navigate with mouse side buttons.** Back and Forward revisit Framekeep views with their filters and scroll positions during the current session. Active filter chips show which filters are applied and let you clear them individually.
- **See feedback while the gallery loads.** A progress line below the header shows when the gallery is fetching results. Photo cards reserve space for their previews so the layout stays steady as images appear.
- **Collapse Sources to save space.** The sidebar keeps a consistent width when Sources opens or closes. The list can be scrolled with a themed scrollbar, and the add-folder button is available while the list is collapsed.
- **Keep names copyable.** Section labels stay unselected during normal use. File and folder names remain selectable, and their context menus offer copy actions. **Ctrl+C** still copies selected originals to the Windows clipboard when you are working with a gallery selection.
- **See when Framekeep is starting or working.** The four corners of the Framekeep logo expand, rotate and settle around the stationary picture. The progress panel uses the same motion at a slower pace. Framekeep respects the system’s reduced-motion setting.

### Catalog and local file safety

- **Forget removed folders and files.** In **Removed items**, choose **Forget from Framekeep** to clear the catalog record and saved details. The original stays in File Explorer. Use **Restore** before forgetting if you want the existing catalog details back. Re-adding or rescanning the original creates a fresh entry.
- **Recheck files and destinations during processing.** Framekeep checks that a source stays within its registered folder and that hashing and decoding refer to the same file. It skips linked entries and rechecks the approved destination when writing verified copies.
- **Limit local requests and video processing.** Session setup limits the amount of data a request can send. Video preparation checks MOV and MP4 headers before invoking FFmpeg, restricts it to local input and uses time, memory and output limits. On Linux, the default app data directory is restricted to the current user.

See the [README](../README.md) for everyday use and the [technical reference](TECHNICAL.md) for processing limits and local data locations.
