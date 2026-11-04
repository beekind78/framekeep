# Publishing source safely

The repository contains application source, tests, documentation and generated demo artwork. The running photo library is separate from the checkout and must remain on the owner's PC.

Before creating a commit, stage only the intended source files and run:

```powershell
python scripts/check_publication.py --staged
git diff --cached --stat
git diff --cached --check
```

After committing, `python scripts/check_publication.py` checks HEAD. It reads Git's actual blobs, not the working directory, and rejects unexpected paths, binary files, private keys, common credential formats and non-example Windows profile paths. The only allowed binary assets are the generated demo screenshot, Framekeep icon and licensed DM Sans font. Their SHA-256 hashes are pinned in the checker. Replacing an asset requires inspecting the replacement and updating its expected hash deliberately.

The ignore rules exclude personal image/video formats, databases, credentials, model files, exports, caches and local diagnostics. The explicit screenshot exception contains generated landscapes only. Tests generate their own temporary media and do not need personal photos.

GitHub Actions repeats the source checks and tests after a push. This is a regression check, not a substitute for inspecting the staged snapshot before uploading. A newly added ignore rule does not remove a previously committed file or erase Git history.

The desktop library, face data, thumbnails, playback cache, runtime credentials and private test artifacts are not release artifacts. Keep original folders and organized outputs outside the source checkout. Publish documentation screenshots only after checking both their visible contents and file metadata.
