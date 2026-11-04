# CleanGuard live demo (about 4 minutes)

Rehearsed on 28 Oct 2026, MacBook Air M3 8 GB: scan of 14 files took **1 min 40 s**.
Run every command from the `Harness/` folder with the venv active (`source .venv/bin/activate`).

## 10 minutes before

File ages count from the day the testbed was generated (28 Oct). From about 8 Nov the
"recent" file is over 30 days old and no longer kept by the recent rule, so for the final demo
regenerate the testbed first: `python eval/make_testbed.py --seed 7 --out testbed/ --force`.

```bash
python demo/make_demo.py --force          # fresh 14-file folder in demo/files/
curl -s localhost:11434/api/generate \
  -d '{"model":"qwen3:4b-q4_K_M","keep_alive":"60m","options":{"num_ctx":8192}}' > /dev/null
ollama ps                                 # must say 100% GPU and 8192
```

`num_ctx` must be 8192 (the same as config.yaml), or the first scan reloads the model and
takes about 17 s longer. Close other apps: 8 GB is tight. Keep the rehearsal report open in
a browser tab as a backup.

## On stage

| # | Command | Say |
|---|---|---|
| 1 | `python -m unittest discover -s tests` | 58 safety tests in one second, no model needed. |
| 2 | `python -m cleanguard scan demo/files` | Start it, then talk while it runs (~100 s). Rules decide the duplicate, the unfinished download and the recent file instantly; the model labels the rest. |
| 3 | `open runs/<run_id>/report.html` | Deletable sorted by size, then Review, then Sensitive with a type badge. The bank statement and ID card are flagged and kept. `scan_016.pdf` has a generic name but a fake Thai ID inside: it lands in Review, never Deletable. That is the bug we fixed. |
| 4 | `python -m cleanguard approve <run_id>` | Type `1-5`, press Enter at the review prompt, then type `MOVE`. Point out: nothing moves without the typed word, and the token is one-time. |
| 5 | `ls demo/files/Downloads` | The installers, the copy and the partial download are gone. |
| 6 | `python -m cleanguard restore <batch_id>` | Everything comes back, byte for byte. CleanGuard never deletes. |
| 7 | `tail -3 logs/calls.jsonl` | The log has file IDs only: no names, no content. |

Copy the `<run_id>` and `<batch_id>` from the output of steps 2 and 4.

## Expected result (rehearsal)

| Label | Files |
|---|---|
| deletable (6) | 3 installers, `Copy of zoom…` (rule: exact copy), `video_2.mp4.download` (rule), `essay_draft1.docx` |
| review (2) | `scan_016.pdf` (fake ID inside), `scan_040.pdf` |
| sensitive, keep (2) | `bank_statement_aug.pdf`, `id card nattapong.txt` |
| keep (4) | CV, essay final, lecture slides, recent installer zip (rule: 19 days old) |

The model runs at temperature 0, so labels should match; the reasons may be worded slightly
differently.

## If something goes wrong

- **Ollama not running / model missing:** the run ends in FAILED with nothing moved. That is
  itself a safety demo; then show the rehearsal report instead.
- **Scan slower than 2 minutes:** go on with slide 5 and come back to the report.
- **Wrong numbers typed at approve:** press Ctrl+C. The run ends with nothing moved; start a
  new scan or show the rehearsal.

## After

`python demo/make_demo.py --force` rebuilds the folder. Restored quarantine batches stay listed
in `python -m cleanguard runs` (0 files); their folders are in
`~/Library/Application Support/CleanGuard/quarantine/`.
