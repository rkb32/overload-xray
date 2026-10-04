# Launch notes: a product needs a user, a problem and a way to reach them

## Who it is for
A backend or SRE engineer who has just lived through (or fears) a retry storm: a service slows down, callers time out and retry, the load multiplies and the system does not recover. They have traces. They do not know how much of their load is wasted work.

## The problem, with evidence from this year
- Uber published how it protects against retry storms (September 2026): a Nov 2025 outage, 9.5 million spurious requests prevented, and an admission that plain retry budgets lack visibility into cross-service amplification.
- GitHub's August 2026 eight-hour outage was blamed on an autoscaling failure plus a retry storm.
- The Hacker News discussion of Uber's post centered on deadline budgets passed between services and on deciding which hop owns an error. I did not verify that anyone there asked for a tool that measures wasted work: that is a question to ask them.

No tool found in two searches measures goodput and zombie work from real traces. That is a gap, not a guarantee: check again before claiming it.

## The hook
A number and a story, not a feature list: "I measured how much of a system's work is wasted when callers give up." The current number comes from a synthetic load test (users succeed 97% of the time at 8 requests per second with the full fix, against 35% without). It becomes much stronger with a measurement on a real open-source microservice app (Online Boutique or the OpenTelemetry demo). That is not done yet.

## What we hand them (and what we do not)
- **A number:** how much of their work was never used, and how much of it ran after callers had left.
- **A named fix per pattern:** retry storm → a retry budget (about 10% of traffic), backoff with jitter, stop retrying once the deadline has passed. Zombie work → send the caller's remaining time downstream and cancel at the deadline. Doomed work → refuse jobs that cannot finish in the time left.
- **A reference implementation of the last two:** `demo/deadline.py` (an ASGI middleware that reads `x-deadline-ms`, cancels at the deadline and answers 504) and the shedding check in `demo/service_b.py`.
- **The retry map:** which calls were attempted again, how many layers did it, and what that multiplied ("payments received 9 calls for one call to orders"), plus the writes that were retried and might have happened twice. The question to put in a first message: "what is your retry multiplication?"
- **A way to prove it worked:** upload the traces again after the change and compare (`xray compare`).

Not offered yet: an installable library for their stack, automatic code changes, or evidence from a real system. The "35% → 97% succeed" result is from synthetic services on one machine. Say that in the first message, before they ask.

## Before launching
1. Finish the deploy ([DEPLOY.md](DEPLOY.md)): it is live and checked; what is left is the AWS Support case to raise the Lambda concurrency quota, then reserving a few slots for this function so a flood cannot starve Winnow.
2. Measure a real app and put the result at the top of the page.
3. Get 3 to 5 engineers who own real traces to try it. Success is a person who uploads their own file and tells you what they saw, not stars.

A link that shows a result without asking the reader to do anything: `<address>/#sample=retry-storm` opens the page with the bundled sample already analyzed. Use it in the first message instead of "go try it".

## Where to find them
r/devops and r/sre, the OpenTelemetry community, Hacker News (a Show HN with the number), LinkedIn posts by SREs about recent outages, and the people who wrote the retry-storm posts and papers. Open with a question (how do you measure wasted work today?), not with "I built a tool".

## What not to claim
- Not "novel": goodput, deadline propagation and load shedding are known ideas. The contribution is measuring them from traces and showing that cancelling alone is not enough.
- Not "validated on real systems": it has not been.
- Not "production ready": it is a first version with the limits listed in the README.
