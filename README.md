# JOZOOR Studios publisher

Posts approved JOZOOR Studios Reels to Instagram (@jozoor.om) on schedule via the
official Instagram API. See `publish.py` for how duplicates are prevented.

- `queue/<id>/` an item: video.mp4, cover.jpg, caption.txt, meta.json (`approved`, `publish_at`)
- `state/ledger.json` what has been published (written by the workflow)
- `state/token.enc` encrypted access token (key held in the `TOKEN_KEY` secret)
