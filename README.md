# reel2txt

A small web app for your Linux box. Open it in Chrome from any of your devices on Tailscale,
paste video links (TikTok, Instagram, YouTube, X...), and the box does the rest:

1. downloads the video (`yt-dlp`)
2. reads the post caption and hashtags
3. turns speech into text (`faster-whisper`, English and Japanese, on the graphics card if it works)
4. grabs frames, and reads on-screen text (OCR)
5. writes a one-line summary using your local Ollama model
6. files it all in one folder and updates the library list (`index.html` and `index.txt`)

On each item's page, **Copy for Claude** copies a ready-to-paste block that also asks Claude to find the
related chat. Attach a few frames from the `frames` folder if the picture matters.

## What it can read
- **Videos** (TikTok, Instagram, YouTube, X and many more): downloaded, transcribed, frames and on-screen text, summary.
- **Text and image posts** (X, Instagram carousels): text and pictures, with OCR.
- **Web pages and PDFs** (v2.4): readable text is pulled out and filed like everything else.
- **Reddit threads** (v2.5): the post plus its comments.
- **Anything that will not read** (v2.6): the box prints the page to PDF in a headless browser and reads that.
- **Files already on the box**: Import page, videos, pictures and PDFs.
- **Sorting**: every item gets project(s) and a category. `/apply` takes a sorting list from Claude, and `/for-claude.txt` is the file to paste into a chat.

## Pages
`/` add links, `/index.html` full library, `/projects.html` by project then by category, `/for-claude.txt` for chat,
`/apply` sorting file, `/edit-projects`, `/edit-categories`, `/import`, `/index.txt`.

## Install (on the Linux box, once)

1. Copy this `reel2txt` folder to the box (for example into your home folder).
2. In a terminal on the box:
   ```
   cd ~/reel2txt
   bash install.sh
   ```
   It installs the packages, builds its own Python environment, and starts a service that
   comes back by itself after every reboot. It takes several minutes.
3. Open `http://<the box's Tailscale address>:8090` in Chrome.

The first link is slower because the speech model downloads once.

## Updating from GitHub
First time on a box that already has the app (your `venv`, `config.env` and data are not touched):
```
cd ~/reel2txt && git init -b main && git remote add origin https://github.com/oo0speed0oo/reel2txt && git fetch origin && git reset --hard origin/main && bash update.sh
```
After that, every update is:
```
cd ~/reel2txt && git pull && bash update.sh
```
`update.sh` installs any new helpers and restarts the service. Your `projects.md` is private to your box (it is in `.gitignore`);
`projects.example.md` is only the starting list for a new install.

## Tips
- **Reddit or another site blocks the box:** the app prints the page to PDF itself. If that also fails, print it in your own browser
  (Save as PDF), put the PDF in a folder on the box, and use `/import` with "Include PDF documents".
- **Browser for page-to-PDF:** `update.sh` installs it. If it says system libraries are missing: `sudo ./venv/bin/playwright install-deps chromium`.
- **Instagram:** the reliable way is Firefox logged in on the box (or a cookies file). Automatic password login usually fails.
- **A link fails:** press Retry. Still failing: the row shows the reason, and `journalctl --user -u reel2txt -n 50` has more.
- **Sorting is off:** change it on the item page. The local AI learns from your recent corrections.
- **Paste a Google search link?** It cannot be read. Open the result page and paste that link.

## Instagram

- On the box, open Firefox and log into the throwaway Instagram account once. Leave it logged in.
- reel2txt reads that login from Firefox (`R2T_COOKIES_BROWSER=firefox` in `config.env`).
  No password is stored anywhere.
- If Firefox cookies can't be read, it tries again without login, which works for many public reels.
- Instagram may rate limit or ask the account to verify. If a link fails, wait and press Retry.
- Alternative: export a `cookies.txt` with a browser extension and set `R2T_COOKIES_FILE`.

## Where things are

Everything goes to `~/reel2txt-data` (change `R2T_DATA` in `config.env`):

```
index.html, index.txt       the full list, updated after every link
library/<date_uploader_id>/
    index.html              item page: summary, frames, video, copy button
    summary.md              the paste-ready block
    transcript.txt          full transcript with times
    frames/  video.mp4  meta.json
```

The page shows free disk space. It refuses new downloads below 5 GB free (`R2T_MIN_FREE_GB`).

## Settings

Edit `config.env`, then run `systemctl --user restart reel2txt`. The file explains each setting.
Useful ones: `R2T_WHISPER_MODEL=medium` (better Japanese, slower), `R2T_OLLAMA_MODEL` (which model
writes summaries), `R2T_PASSWORD` (adds a password to the page).

## Day to day

- Status: `systemctl --user status reel2txt --no-pager`
- Log: `journalctl --user -u reel2txt -n 50 --no-pager`
- Update the downloader when a site stops working: `~/reel2txt/venv/bin/pip install -U yt-dlp`

## Things to know

- There is no login on the page by default. Keep it on Tailscale only. Do not forward a port to it.
- If the graphics card can't be used for speech, it falls back to the CPU on its own (slower).
- YouTube sometimes needs an extra JavaScript runtime for `yt-dlp`. TikTok and Instagram don't.
- Port 8090 was chosen so it doesn't clash with Open WebUI (3000), Command Center (8080) or the JLPT app (8000).

## What's new in v2

- **Posts without video.** X text and image posts, and Instagram image posts and carousels, now work.
  The app saves the text, downloads the images, reads text in the images (OCR) and files it like a video.
  X posts are read from X's own public embed data, then `gallery-dl`. Instagram image posts use the
  burner account's Firefox login through `gallery-dl`. Set `R2T_ALLOW_THIRD_PARTY=1` in `config.env` only
  if you also want the app to ask fxtwitter.com when X refuses.
- **"You can use this in..."** After each link, the local AI (Ollama) picks up to two of your projects it could
  help, with one sentence why. If Ollama is off it falls back to keyword matching.
  - Every item page shows it and lets you change it by hand. Your change is kept.
  - `projects.html` lists every link grouped by project, with an Unsorted group.
  - `index.html` and `index.txt` have a "Use in" column.
  - The Copy for Claude block now lists where the app thinks it fits.
- **Edit your projects** at `/edit-projects` (the page has a Save button and can re-sort the whole library).
  The list is `projects.md` in the data folder. Give the same file to any AI so it knows what you work on.
- Audio is now decoded with `ffmpeg`, which fixes the "metadata_errors" error.

## Updating an existing install

Copy `reel2txt-update.zip` to the box, then:

    cd ~ && unzip -o reel2txt-update.zip && cd reel2txt && bash update.sh

Your `config.env` (port, settings) is not touched, and your library and queue are kept.

## Import files you already downloaded (v2.1)

Open `/import` (there is a link on the main page).

1. Type the folder on the box (it starts at `~/Downloads`), tick what you want, press **Scan this folder**.
   It shows how many new videos and pictures it found and how big they are. Nothing is queued yet.
2. Press **Import**. Each file goes through the same steps as a link: speech to text, frames, on-screen text,
   summary, and "You can use this in...". They run one after another in the queue.
- Your original files are only read, never changed, moved or deleted.
- Only folders inside your home folder can be imported (`R2T_IMPORT_ROOT` in `config.env` changes that).
- Files already imported are skipped, so you can scan the same folder again later.
- If a video has a `NAME.info.json` or `NAME.description` file next to it (yt-dlp and similar tools make
  these), the app uses the real title, uploader, caption and original link.
- "Copy the videos into the library" is off by default. With it off, the library keeps the transcript,
  frames and summary, and records where the original file is.

## Instagram login that looks after itself (v2.2)

Run this once on the box:

    cd ~/reel2txt && bash set-instagram.sh

It asks for the burner account's username and password (the password is hidden as you type) and saves them
in `~/.reel2txt-secrets`, readable only by your user. After that:
- On the next Instagram link, the app signs in by itself and saves session cookies.
- It renews the session about every 6 days, and signs in again once if a link fails with a login problem.
- The page's bottom line shows "Instagram: logged in..." or the reason a login failed.
- The password is never shown on a page, never put on a command line and never written to the log.
- To remove everything: `bash remove-instagram.sh`.

Use only a throwaway account with a password you use nowhere else. Turn two-step login off on that account,
because the app cannot type a code. If Instagram asks the account for a security check, log into it once in a
normal browser to clear the check, then retry.


## v2.3: categories and "Apply a sorting file"
- Every item now has a project (or none) AND a category. Items that help no project are listed by category on `/projects.html`.
- `/apply`: paste lines like `folder | project 1; project 2 | category | why` (use `-` for no project). Items set this way are never changed by the automatic re-sort.
- `/for-claude.txt`: one file with instructions, your project and category lists and every item. Paste it into a chat to get sorting back in the format `/apply` reads.
- `/edit-categories` edits the category list. New suggested projects are added to your projects list automatically (yours are never overwritten; a `.bak` is saved).
- The local AI now sees your recent hand-made sorting as examples.


## v2.4: web pages and PDFs
- Paste any article or PDF link into the main box. If it is not a video or a social post, the app reads the page (menus and footers are dropped) or the PDF and files it like everything else: summary, project, category, `text.txt`, and a copy of the PDF (`original.pdf`) plus a first-page picture.
- Scanned PDFs are read with OCR (the first 15 pages; Japanese and English). Long PDFs: the first 400 pages are read.
- Import page: tick "Include PDF documents" to bring in PDFs already on the box.
- Limits: pages that need JavaScript or a login show a clear error; files over 60 MB are skipped. Settings (optional, in config.env): `R2T_DOC_MAX_MB`, `R2T_PDF_PAGES`, `R2T_PDF_OCR_PAGES`.
- `update.sh` installs the one new helper (`pymupdf`). Run it as before.

## v2.5: Reddit threads
- Paste a Reddit post link. The app reads the post and its comments (score in brackets, replies indented) and files it as a document. Videos in a post are not downloaded; the thread text is.
- If Reddit blocks the box, the error says what to do: print the thread to PDF in your browser, put it in the import folder, and import it with "Include PDF documents".

## v2.6: pages that will not read get printed to PDF
- If a web page (or a Reddit thread) cannot be read directly, the box opens it in a headless Chromium, scrolls it so comments and lazy parts load, prints it to PDF, and reads the text from that PDF (OCR if it is only pictures). The PDF is kept as `original.pdf`. Items show "Web (printed to PDF)".
- Not used for Instagram, TikTok, YouTube, X, Facebook or Threads (a login wall would just be printed).
- `update.sh` installs the browser (about 150 MB, once). If it says system libraries are missing, run once: `sudo ./venv/bin/playwright install-deps chromium`.
- If a site still blocks even a real browser, the error says so; use your own browser's Save as PDF and Import as before.
