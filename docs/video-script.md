# Theta Warden — Video Script (~2:45)

Read out loud while you scroll. *(Italics = what to do, not what to say.)*

---

*(Top of page)*

> "In this competition we named our trading agent **Theta Warden**. And here we
> are — online, right now.
>
> We made this since **August 31**, running until now. From a hundred thousand,
> we're already up about **a quarter of a percent**.
>
> We modified a bit for the dashboard. But let me explain the logic — how this
> thing actually decides.
>
> **Every fifteen minutes the market is open, it wakes up by itself.** Nobody
> clicks anything.
>
> It pulls the live SPY option chain from Alpaca and builds every possible **put
> credit spread** — sell a put, buy a cheaper put below it as protection. We get
> paid a premium up front, and the most we can lose is the gap between the two
> strikes. We price it conservatively, selling at the bid and buying at the ask,
> so a trade never looks better on our screen than it would actually fill.
>
> That gives us seventy, eighty candidates. And here's the honest part —"

*(This is the part judges remember. Slow down.)*

> "Every one of those candidates comes with the **win rate it needs just to break
> even**. And the market prices that number very close to the true probability. So
> picking the best-looking strike is **not an edge**. It's a coin flip with extra
> steps.
>
> We tell the agent that directly. And we tell it where the real edge actually is
> — only two places.
>
> **One: being selective.** Only trade when the premium overpays for the actual
> risk. Otherwise stand aside. A skip costs nothing.
>
> **Two: exits.** On this strategy a loss is about ten times the size of a win. So
> one loss you avoided is worth many premiums you collected. Cutting a position
> when the reasoning breaks matters far more than picking the perfect strike.
>
> So the agent isn't hunting for a magic strike. It's waiting for the market to
> overpay, and it's watching its exits.
>
> That's also why it skips more than half the time — and that's the design, not a
> bug.
>
> And on top of all that: **the model has the judgment, but the code has the
> veto.** Our code builds the menu and the risk gates filter it. The model only
> chooses from what survived. It can't invent a strike, size past the cap, or go
> naked.
>
> That's the name — **theta** is the premium we collect as time decays, and the
> **warden** is the code behind the model that can always say no."

---

*(Scroll to the chart)*

> "Performance against SPY. SPY's up point eight six this week. We're up point one
> five.
>
> We show that on purpose — because **that gap is the strategy working, not
> failing.** A credit spread trades away the upside to buy a hard floor under the
> downside. On a green week, that's exactly what it should look like.
>
> Look at the shape. Orange is SPY, dropping and spiking. Blue is us, just
> grinding up."

---

*(Scroll to Open Positions)*

> "The open positions. This one sold the 750 put, bought the 745, three contracts,
> five days out — ninety dollars collected, seventy-five already profit.
>
> Blue dot is where SPY is trading. Red is where we start losing. **Three percent
> of cushion** between them.
>
> And underneath, a stop at twice the credit, and it closes one day before expiry
> automatically — no matter what the model thinks."

---

*(Scroll to Risk Gates — slow down)*

> "The risk gates. These aren't instructions in a prompt — they're functions that
> return **false**. You can talk a model out of an instruction. You can't talk it
> out of a function that rejects its trade.
>
> Two spreads per name. Five percent of equity max per trade. A daily loss kill
> switch — entries stop, but exits are never blocked. Ten contracts per order. And
> naked shorts, blocked.
>
> These read live from our config. What's on screen is what's running."

---

*(Scroll to Decision Record)*

> "The decision record. Three trades placed, seventeen blocked by the gates — and
> twenty-six times it stood aside. More than half its decisions. That's on
> purpose.
>
> A hundred twenty-three decisions logged. Thirty-two cents of model spend."

---

*(Scroll to Decision Trail)*

> "And the decision trail. On every entry it commits to a **thesis** and an
> **invalidation** — a specific condition that would prove it wrong. So it can't
> move the line later to justify holding a loser.
>
> Filter to the declines and you can read why it passed on every trade it didn't
> take."

---

*(Back to top)*

> "That's **Theta Warden**. A model placing real options trades on a real account
> since August 31 — with a trail you can audit line by line, and code behind it
> that can always say no."

---

## Before you record

- Agent running so the pill is green. Zoom 110%, 1920×1080.
- Read live numbers off your screen, not this page.
- Two percentages on screen: **+0.29% since $100k** (tile) and **+0.15% since it
  went live** (chart). Both true, different baselines — don't mix them.
- Scroll between sections, never while talking.

## Don't say

- "It beats the market" — it doesn't, and doesn't try to on a green week.
- "It can't lose" — it can. Capped and known is the point.
- "Fully hands-off" — it's supervised autonomy. The supervision is the pitch.
