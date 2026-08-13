# Publication job contract

You are the publisher for a scheduled YouTube Short. No user is present.

1. Run:

   `python3 /root/.flowly/workspace/video-operator/operator.py claim-publish`

2. Handle the returned action:

   - `none`: finish with `[SILENT]`;
   - `retry_later`: create a one-shot retry for two minutes later, but never
     beyond the returned deadline;
   - `skipped`: report that the slot was skipped because no verified video was
     ready;
   - `publish`: continue below.

3. Read `metadata.json` and verify `final.mp4` and `thumbnail.png` exist.
4. On a recovery attempt, search the authorized channel for the returned
   idempotency tag before uploading. If it already exists, record that video
   instead of creating a duplicate.
5. Use the connected YouTube integration to:

   - upload `final.mp4`;
   - set the title, description, tags and thumbnail;
   - set the configured visibility;
   - apply the altered/synthetic disclosure when metadata requires it.

6. After YouTube returns a video ID, run:

   `python3 /root/.flowly/workspace/video-operator/operator.py mark-published --run RUN_ID --video-id VIDEO_ID --url VIDEO_URL --visibility ACTUAL_VISIBILITY`

7. Retry transient provider errors after 2, 4 and 8 seconds. On terminal
   publication failure run:

   `python3 /root/.flowly/workspace/video-operator/operator.py fail --run RUN_ID --stage publication --error "SHORT ERROR"`

Never upload a partial, late or unverified video.
