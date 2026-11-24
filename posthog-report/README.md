Posts MySky's weekly PostHog numbers to Discord every Monday.

# Report's row definitions

Each number is for one whole week: the one named in the column heading.

Weeks run Monday to Sunday, US/Pacific.

A like, repost or reply only counts if it was made on a post a MySky feed served to that person, in the feed or after opening the post from it.

The team's own use is left out of every metric (via PostHog's "internal and test users" filter), 'Setting changes' included.


| Term in the post    | Meaning                                                     | PostHog definition                                          |
|---------------------|-------------------------------------------------------------|-------------------------------------------------------------|
| Weekly active       | People who saw[^1] at least one post                        | Distinct people with `interactionSeen`                      |
| Daily active (avg)  | People who saw[^1] at least one post, averaged over days of the week       | Distinct people with `interactionSeen` each day, averaged over the 7 days |
| New users           | People who saw[^1] their first MySky post ever              | People whose earliest `interactionSeen` is in the week      |
| Feed loads          | Times a feed was opened (scrolling further is not counted)  | `feedLoaded` first pages asking for more than 8 posts       |
| Feed users          | People who opened a feed                                    | Distinct people behind Feed loads                           |
| Explore loads       | Times Bluesky's Explore page previewed a feed               | `feedLoaded` first pages asking for 2 to 8 posts            |
| Explore users       | People who got such a preview                               | Distinct people behind Explore loads                        |
| Likes               | Like taps on posts a feed showed (unlikes not subtracted)   | `interactionLike` events                                    |
| Reposts             | Repost taps on posts a feed showed (quotes not included)    | `interactionRepost` events                                  |
| Replies             | Taps on the reply button, whether or not a reply is posted  | `interactionReply` events                                   |
| Show more           | Taps on "Show more like this"                               | `requestMore` events                                        |
| Show less           | Taps on "Show less like this"                               | `requestLess` events                                        |
| Setting changes     | Feed settings changed and saved on the MySky website        | `feedControlChanged` events, one per setting changed        |
| Settings users      | People who changed and saved at least one feed setting      | Distinct people with `feedControlChanged`                   |
| 8 Weeks             | One bar per week, oldest left, scaled to the row's largest  | Not applicable                                              |
| A date (e.g. 11-09) | The count for the week starting that Monday                 | Not applicable                                              |
| vs Prev             | Percent change from the week before; green good, yellow not, no colour under 10% | Not applicable                                              |

[^1]: "Saw a post" means it stayed on screen for half a second.

# usage 

```bash
python posthog-report/report.py           # print the table
python posthog-report/report.py --post    # post it to Discord
```

Needs `POSTHOG_API_KEY`, `POSTHOG_PROJECT_ID` and `DISCORD_WEBHOOK_URL`: repo
secrets for the Monday run (`.github/workflows/posthog-report.yml`), or a
gitignored `.env` beside `report.py`. This repository's Action logs are public,
so the workflow only posts and never prints the numbers.
