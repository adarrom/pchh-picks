# Pop Culture Happy Hour picks

A personal list of the movies and TV shows recommended in NPR Pop Culture Happy Hour's
"What's Making Us Happy" segment (2026 onward). A GitHub Actions job runs daily. It reads
NPR's podcast feed and transcripts, uses Claude to pick out the movie/TV recommendations,
adds poster links, and commits `picks.json`. A personal Android TV app reads that file
to show a home-screen row.

- `update.py`: the job (`python update.py --dry-run` checks the feed and transcripts without AI calls)
- `.github/workflows/update.yml`: daily schedule, plus a manual "Run workflow" button
- Needs one repository secret: `ANTHROPIC_API_KEY`
- Episodes whose transcript never appears within 21 days are skipped.

Not affiliated with NPR. Titles and short descriptions only; no transcript text is stored.
