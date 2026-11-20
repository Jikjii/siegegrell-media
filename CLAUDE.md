# Rules for this repo

This repo is public. It hosts finished chart cards for @SiegeGrell posts so Postiz can fetch them by URL.

- Render cards with `python3 -I make_card.py <spec.json>` from the repo root. Read the PNG to check it before anyone sees it.
- If the script exits with code 2, shorten the field it names. Never cut a fact to make it fit.
- Commit only cards Geraldo approved, into `cards/`. Push straight to `main` with `git -c push.negotiate=true push origin main`. No branches or pull requests needed.
- Never force-push, rewrite history, or delete a card that was posted.
- Never commit photos or video frames from the web, transcripts, drafts, specs, notes or credentials. Press photos go to Postiz by their own URL and are never copied here.
- Change `make_card.py` only when Geraldo asks, and re-render the examples to check the layout after any change.
