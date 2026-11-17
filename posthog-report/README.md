# posthog-report

Posts MySky's weekly PostHog numbers to Discord every Monday, as two boxes.

Every row is about MySky feeds inside the Bluesky app. Weeks run Monday to
Sunday, US/Pacific. "Saw a post" means it stayed on screen for half a second.

| Term in the post   | Meaning                                                    | Counted as                                                   |
|--------------------|------------------------------------------------------------|--------------------------------------------------------------|
| Weekly active      | People who saw at least one post during the week           | Distinct people with `interactionSeen`                       |
| Daily active (avg) | People who saw at least one post, on an average day        | Distinct people with `interactionSeen` per day, summed, / 7  |
| New users          | People who saw their first post ever during the week       | People whose earliest `interactionSeen` is in the week       |
| Feed loads         | Times a feed was opened (scrolling further is not counted) | `feedLoaded` first pages asking for more than 8 posts        |
| Feed users         | People who opened a feed                                   | Distinct people behind Feed loads                            |
| Explore loads      | Times Bluesky's Explore page previewed a feed              | `feedLoaded` first pages asking for 2 to 8 posts             |
| Explore users      | People who got such a preview                              | Distinct people behind Explore loads                         |
| Likes              | Like taps on posts a feed showed (unlikes not subtracted)  | `interactionLike` events                                     |
| Reposts            | Repost taps on posts a feed showed (quotes not included)   | `interactionRepost` events                                   |
| Replies            | Taps on the reply button, whether or not a reply is posted | `interactionReply` events                                    |
| Show more          | Taps on "Show more like this"                              | `requestMore` events                                         |
| Show less          | Taps on "Show less like this"                              | `requestLess` events                                         |
| 8 Week Summary     | One bar per week, oldest left, scaled to the row's largest |                                                              |
| Week of MM-DD      | The count for the week starting that Monday                |                                                              |
| Change vs Prev     | Percent change from the week before; green good, yellow not |                                                             |

Our own uptime check is left out of the load rows. The team's own use is not.

```bash
python posthog-report/report.py           # print the boxes
python posthog-report/report.py --post    # post them to Discord
```

Needs `POSTHOG_API_KEY`, `POSTHOG_PROJECT_ID` and `DISCORD_WEBHOOK_URL`: repo
secrets for the Monday run (`.github/workflows/posthog-report.yml`), or a
gitignored `.env` beside `report.py`. This repository's Action logs are public,
so the workflow only posts and never prints the numbers.
