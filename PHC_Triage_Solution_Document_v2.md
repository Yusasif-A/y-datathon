# PHC Triage: Solution Document (corrected merge)

**YoungDataPro Datathon 2026, Health Track** · Updated Saturday 10 Oct 2026

**Deadline check:** the brief says "Saturday, October 11, 11:59 PM WAT," but 11 Oct 2026 is a Sunday. Confirm with the organisers. Until then, treat **tonight (Sat 10 Oct, 11:59 PM WAT)** as the safe deadline and submit hours early.

**How to read the labels:** **Build** = working in the demo. **Mock-up** = shown as a design or screenshot only; do not claim it is built. **Later** = described as a next step. All sample or synthetic data is labelled "Sample data."

---

## 1. The problem in plain words

Nigeria has about 31,000 primary health centres (PHCs), most people's first contact with the health system. Facilities are supposed to report upward (facility, LGA, state, then DHIS2), but reports arrive late, incomplete or not at all. So officers and planners are guessing, and a medicine can run out with nobody above the facility knowing until a patient arrives and there is none.

**Evidence of the gap:** the 2023 National Health Facility Survey (NBS) found an average of 34.3% of essential drugs available in primary facilities (the share of essential drugs present and not expired, not the share of facilities). It sampled 3,330 facilities, and state figures ranged widely (for example about 54% in Delta against about 14% in Kebbi). *Source: news reports of the NBS report; check the report itself before quoting.*

An LGA officer looks after roughly 40 facilities (our own estimate from about 31,800 PHCs across 774 LGAs) and can visit only a few each week.

**The question our tool answers:** *"Which facilities should I check first this week, and why?"*

## 2. The solution in one paragraph

The tool reads the data the system already collects (DHIS2-style monthly reports), treats late, falling and missing reports as warning signs, estimates which facilities are likely to run short of medicines, vaccines, staff or critical supplies in the next 1 to 2 months, multiplies that by how many people would be hurt, and gives the officer a short ranked list. Every flag has a plain-language reason and a suggested action. *(A weekly WhatsApp version of the list is shown as a mock-up.)*

**One line for judges:** *"Facilities already report into DHIS2, but unreliably. We read that data, treat late and missing reports as warning signs, and tell officers which facilities to help first. We add a layer on top of DHIS2; we do not replace it."*

## 3. Relationship to DHIS2 and to existing tools (judges will ask)

| Question | Answer |
|---|---|
| Do we replace DHIS2? | No. |
| Do we write data back into DHIS2? | No. DHIS2 is a read-only source. |
| Where does our output go? | Our own dashboard (and, as a mock-up, officers' WhatsApp). Never into DHIS2. |
| Do facilities have to do anything new? | No, in the core tool. |
| What about the demo data? | Real facility-level DHIS2 data is not public, so we use synthetic data shaped like DHIS2 (data element, facility, month, value), clearly labelled. Real data can replace it later. |
| What about existing dashboards? | A vaccine stock visibility dashboard and the NPHCDA PHC Functionality Dashboard show *what was reported or assessed*. They do not rank who to visit first, interpret silence, or weigh people affected. They are potential data inputs, not rivals. Mini-DHIS (a phased NPHCDA rollout for real-time PHC data) is the data-capture layer; we are the triage layer on top. *(Contents of these tools not verified; confirm before claiming gaps.)* |

## 4. The whole flow

**Today:** the in-charge fills a paper monthly form, the LGA records officer types it into DHIS2, and state and national levels see it. It breaks because forms are late or missing, DHIS2 mostly holds service counts (not medicine stock), and the officer cannot tell which of about 40 facilities to visit first.

**With our tool:**

```
Facility -> paper form -> LGA types into DHIS2 -> (DHIS2-style data)
                                                      |
                                              OUR TOOL reads it
                                  (expected-report check, silence type,
                                   trouble forecast, people affected)
                                                      |
                              Ranked list with reasons + suggested action
                                                      |
                                      Our dashboard (BUILD)
                              WhatsApp weekly brief (MOCK-UP)
                                                      |
                 Officer checks, then records the result
        (Confirmed / Cleared / Just late / Couldn't verify)
                                                      |
                      Tool learns from the answer (feedback loop)
```

**One facility's story:** Ward 4 Health Centre reported every month for 24 months, then went silent for 2. Only this facility is silent (not a whole-LGA typing backlog). It serves about 9,000 people, has unreliable power, fewer nurses than the Level 2 standard, and the next facility is 18 km away. Result: it is near the top of the list with the reason *"reported every month for 2 years, silent for 2, unreliable power, nearest alternative 18 km"* and the action *"call the officer-in-charge, then verify through the ward committee."* The officer calls, finds the vaccine fridge has failed, and records **Confirmed**.

## 5. How the problem statement is covered

| Part of the brief | What the tool looks at | What it does |
|---|---|---|
| Highest-risk facilities | All signals combined into one score | Ranked list sized to the officer's weekly capacity |
| Medicines | Drops in stock-dependent services (e.g. family planning), stock reports where they exist, distance, funding status | Chance that key medicines are short (stock is mostly inferred; we say so) |
| Vaccines | Immunisation counts, vaccine stock and discards, fridge status and power | Vaccine and cold-chain risk as its own failure type |
| Staff | Staff vs Level 1/2 standard, sudden volume drops with late reports, insecurity | Chance of understaffing or closure |
| Critical supplies | Power, water, equipment, connectivity | Adds infrastructure risk |
| Before it becomes a crisis | Reports drifting later, falling volumes, incomplete forms, plus a 1 to 2 month forecast | Flags earlier than "no report in two months" |
| People who rely on them | Population served, services at stake, distance to next facility | Ranks by chance of trouble x people affected |
| The reporting blind spot | Silence patterns and whole-LGA clusters | Separates one troubled facility from an LGA-wide data-entry backlog |

The brief's three suggested approaches are all covered: a risk-scoring model, a geographic view of under-resourced LGAs (State overview), and anomaly detection where reporting gaps are the warning sign (silence types).

## 6. How it works behind the scenes

### 6.1 Data

- **Real:** facility locations and boundaries (GRID3, Health Facility Registry); population grids (WorldPop/GRID3) for people served; NHFS 2023 and SDI 2013-14 as anchors for how likely facilities are to lack medicines (published figures; facility-level files not yet checked).
- **Real but hard to get:** facility-level DHIS2 reports and NHLMIS stock records. Described as the pilot-stage plan.
- **Synthetic:** monthly reports and 4 to 5 tracer items (an antimalarial, a maternal drug, a family-planning commodity, a vaccine, oral rehydration) from a simulator. Always labelled.

### 6.2 Models (build in this priority order)

1. **Expected-report model (build).** Given a facility's history, season and similar facilities, what should it have reported? A big gap is a warning sign. Gradient-boosted model.
2. **Trouble forecast (build).** Calibrated chance of running short of medicines, vaccines or staff in the next 1 to 2 months, using model 1's output, service counts that depend on stock, structural traits and recent trend.
3. **Silence type (rules first, classifier if time).** Sorts each silence into a cause and tells the officer what to do:

| Silence type | What the officer is told |
|---|---|
| Late data entry | Chase the paperwork; not urgent |
| Chronic non-reporter | **Scheduled support check and help to report. Not "low priority."** These are often the least-resourced facilities |
| Sudden drop | Call the officer-in-charge soon; may be a real problem |
| LGA-wide backlog | Send to the LGA data office, not 12 separate visits |
| Closure | Confirm status and redirect patients |
| *Modifier: insecure area* | Escalate; never suggest a visit |

**Ranking (not a model):** priority = chance of trouble x people affected. Show the top k, where k is the number of visits the officer can make.

**Explanations:** each flag gets one plain sentence and a suggested action from its strongest factors (SHAP if time, otherwise the top model features).

**Baseline to beat:** the simple rule "no report for 2 months." If the models cannot beat it, they add nothing. We also report how strong the link between trouble and reporting problems must be for the tool to win.

### 6.3 Inputs versus answers (avoiding leakage)

| Item | Source | Role |
|---|---|---|
| Monthly reports (counts, lateness, completeness), facility traits | Simulated | Inputs for all models |
| True stock, closures, failures in the next 1 to 2 months, the real cause of each silence | Simulated hidden truth | Answers for training and grading; never inputs |
| The simulator's reporting formula | Inside the simulator | Never given to the models |
| Predicted probabilities and silence types | Model output | Feed the ranking |

Train on earlier months, test on later ones, and hold out whole LGAs.

### 6.4 The simulator in short

1. Create facilities with real locations and traits (rural/urban, power, staff, connectivity, size, insecurity).
2. Play out a hidden truth month by month (stock in and out, late funding, fridge failure, staff leaving, closures).
3. Turn truth into messy reports (struggling facilities report late, incompletely or not at all; whole LGAs sometimes go quiet).

The link between trouble and reporting problems (called `c`) is unknown, so we run weak, medium and strong values. Calibration anchors: PHC essential-drug availability around 34% nationally and widely varying by state (NHFS 2023), national reporting rate around 80% (2021 annual report). All other parameters are assumptions, listed in the Simulator Specification. For this deadline, use a reduced world: 500 to 1,000 facilities, 24 to 36 months, 2 states.

### 6.5 Validation to report (all labelled "synthetic data")

Precision@k (of the top k flagged, how many really had a problem), comparison with the baseline, calibration, one stress test on a different fake world, and a rural-versus-urban check.

### 6.6 Fairness rules

- A missed report raises a "check in with them" flag, never a blame label.
- **Missing or thin data is never treated as evidence that a facility is fine.** New facilities with little history are shown with a "low confidence" tag, capped so they cannot flood the list.
- A rural facility is not flagged just for being rural or poorly connected. Connectivity is a model input and each facility is compared with its own history first.
- Scores mean "review priority," not a verdict. Officers can override.

### 6.7 Role of the AI assistant (LLM)

Numbers always come from code, never from the model. It writes the weekly brief, answers questions by turning them into fixed filters over the risk table (read-only), and drafts verification messages. A human approves everything before it is sent. The app works without the LLM using fixed templates. **Build the template weekly brief first; add the LLM only if time allows.**

## 7. The interface

One web app with three views plus a printout. All views read the same scores, so a status recorded in one updates the others. Every sample value is tagged "Sample data."

### 7.1 Officer list (home) (Build)

Top bar: LGA name, "Data as of [date]," offline-copy label, Print button. Three number cards (facilities, flagged this week, LGA-wide alerts). A warning banner when many facilities go quiet together. A box: "How many visits can you make this week?" that cuts the list to that many. Ranked cards, each with rank, facility name, High/Medium/Low tag, people served, a one-sentence reason, a suggested action, and a status control (7.4).

**Visit order (stretch):** order the top k by priority, then by distance from the LGA headquarters (a simple greedy rule). Skip it if time is short and show only the top k.

### 7.2 Facility page (Build)

Header with priority and people served. A bar chart of monthly reports with a dashed line for the expected level and missing months outlined. A "why it is flagged" panel showing how much each reason pushed the score up. Tiles for staff, cold chain, connectivity, last supply, and days since last report. The silence type, the suggested action, and the status control.

### 7.3 State overview (Build, simple)

Number cards (total PHCs, flagged High/Medium/Low), LGA tiles coloured by priority with the number flagged (a real map from GRID3 boundaries is a next step), and a list of cluster alerts. Tapping a tile opens that LGA's officer list.

### 7.4 How the status control works

The statuses record the **result of a check**. The officer presses one only after finding out what is going on, by phoning the officer-in-charge, visiting, or getting a ward committee reply. The app does not verify anything itself.

| Status | Meaning | What happens to the list | What the tool learns |
|---|---|---|---|
| **Checking** | Waiting for an answer | Stays off the new to-do list | Nothing yet |
| **Confirmed** | A real problem (stockout, fridge failure, staff gone, closed) | Stays on a follow-up list until resolved | A real problem; optionally the problem type |
| **Cleared** | Checked and the facility is fine | Removed; not re-flagged for about 30 to 60 days unless a strong new signal appears | A false alarm |
| **Just late** | Report late or stuck in a data-entry backlog; facility fine | Removed; several in one LGA alert the data office | The silence was an entry delay |
| **Couldn't verify** | No answer or no access | Snoozed | Nothing. Never treated as "cleared" |

**"Mark visited" is not a status.** It is a *method*, recorded with the result as how the check was done (phone call, visit, ward committee SMS). **"Override flag"** means the officer disagrees *without* checking. It needs a short reason, snoozes the flag for 30 days, and counts only as a weak signal, not a label.

**What is saved:** facility, date, score and model version at the time, status, who answered and in what role, method, optional problem type.

**Design for bias:** officers only check what is flagged, so labels come only from the top of the list. In a pilot, add a small random check of a few unflagged facilities each cycle. In the demo the loop is simulated (hidden truth stands in for the officer, with deliberate errors) and shows the mechanism, not how real officers behave.

### 7.5 Printable list and assistant panel

**Printable list (Build):** one page with LGA name, date, top-k facilities with reason and action, and blank boxes to tick the result. For officers with no connection.

**Assistant panel (stretch):** weekly brief, question box, and draft verification message, all from fixed filters and templates.

## 8. Mock-ups only (do not claim these are built)

### 8.1 WhatsApp as a delivery channel

WhatsApp only delivers messages. It is not embedded in the dashboard and does not connect to DHIS2. A link in a message opens **our own dashboard**, never DHIS2.

| To | Message | Purpose |
|---|---|---|
| Officer (weekly) | "Weekly priority list, [LGA]. 1) Ward 4 Health Centre: HIGH. Silent 2 months, about 9,000 people served. Suggested action: call the officer-in-charge. Open full list: [dashboard link]" | Officer gets the answer without logging in |
| Facility in-charge (reminder) | "Hello, we have not received your March report. Please send it to your LGA records officer." | Helps fix late reporting; never blaming |
| Facility in-charge (confirmation) | "Please confirm if your vaccine fridge is working. Reply YES or NO." | A reply is shown to the officer, who decides the status |

**Rules:** an officer approves any message to a facility before it is sent; no patient data; no blame language; keep content minimal (facility codes and counts, not detailed rankings).

**Assumptions and caveats**

- WhatsApp needs a smartphone and data. For low-connectivity facilities, SMS or a phone call is the fallback.
- Messages naming flagged facilities leave our system. Confirm that officers are allowed to use WhatsApp for official work.
- Phone numbers: no public list exists. In a pilot they come from the LGA PHC coordinator and State PHC Board with each person's consent (Nigeria Data Protection Act 2023). In the demo, use fake numbers labelled Sample data.
- Real setup needs a WhatsApp Business account, pre-approved templates, a small backend and per-conversation cost. That takes days, so it stays a mock-up.
- If facilities already report late, a new weekly check-in may also be ignored. That is why the core tool does not depend on it.

### 8.2 Facility in-charge view and urgent button (from Pulse)

Shown as one slide or screenshot. The in-charge sees their own flag status, reporting history and any planned visit, and can send an **urgent need** for a critical item that jumps to the top of the officer's list. A short pre-filled weekly check-in (about 10 tracer items, staff on duty, beds) is optional.

### 8.3 How unreliable facilities are brought in

| Level | What happens | Facility effort | Status |
|---|---|---|---|
| 1. Core | Silence is the warning; the officer follows up and records the result | None | Build |
| 2. Next step | One-question reminder or YES/NO reply, saved to our database and shown to the officer | About 10 seconds, no login | Mock-up |
| 3. Later | Optional short check-in plus the facility's own history page | About 2 minutes | Mock-up |

**Outside check:** for facilities that stay silent, a ward committee member or community health worker confirms by SMS or call whether the clinic is open or the drug is available.

## 9. Scope for the time left

**Tier 1 (must have):**
- Small simulator and synthetic DHIS2-shaped data, labelled
- Baseline rule and one or two ML models (expected-report, trouble forecast)
- Ranked list with reasons and suggested actions, sized to k
- Officer list and facility page with the status control
- Validation: precision@k against the baseline, one stress test, rural/urban check
- One-page write-up and 5-minute video

**Tier 2 (if time allows):** state overview, CSV export, printable list, silence-type classifier, simulated feedback rounds, template weekly brief.

**Tier 3 (mock-up or slide only):** WhatsApp messages, in-charge view, urgent button, visit-order planner, LLM assistant, real map, offline caching, live SMS.

**Pilot plan (state as next steps):** one LGA for four weeks under a data-sharing agreement with the State PHC Board; connect to DHIS2 via API (aggregated facility-level data only); request a vaccine stock extract through NPHCDA; officer answers become real labels and the models are retrained.

## 10. Work plan (adjust to the hours you really have)

| Block | Work | Owner |
|---|---|---|
| 1 (3 to 4 h) | Simulator and baseline; check real data downloads in parallel | Data and simulator |
| 2 (4 to 5 h) | Models, ranking, precision@k against baseline, one stress test | ML |
| 3 (4 to 5 h) | Officer list, facility page, status control, printable list | App and dashboard |
| 4 (3 h) | Write-up, screenshots, 5-minute video, mock-ups | Research, story, submission |
| 5 (2 h) | Buffer, form check, submit | Everyone |

Blocks 1 to 4 run in parallel across teammates. **If the deadline is tonight, do only Tier 1 with a thin dashboard.** Cut from the bottom of the tiers.

## 11. Edge cases and how the tool handles them

| Situation | What we do |
|---|---|
| New facility, no history | Compare with similar facilities only; "low confidence" tag; capped so it cannot flood the list |
| Facility closed or duplicated in the registry | "Confirmed closed" state; removed from the list; flag the registry error |
| Seasonal swings (malaria season) | Compare with the same month last year |
| True zero versus missing report | Treated differently; missing is never assumed to be zero |
| Whole LGA quiet (typing backlog) | Detect the cluster; route to the LGA data office, not many visits |
| Late batch report | Score lateness and completeness separately; re-score when data arrives |
| Rural facility with poor network | Connectivity is a feature; fair comparison; rural/urban results reported separately |
| Chronic non-reporter | Scheduled support check and help to report; never permanently low priority |
| Silence in an insecure area | Suggest "escalate," never a visit |
| Immunisation campaign spikes | Mark campaign months so they are not read as anomalies |
| Staff fear punishment and fake reports | Frame flags as support; plausibility checks; no "worst facility" league tables |
| Too many flags | Show only the officer's weekly capacity; snooze rules |
| Little or wrong officer feedback | "Couldn't verify" is never a label; record the method; audit a sample |
| Real data late or denied | Fall back to synthetic data in the same format |
| Sensitive rankings leaked | Role-based access; no patient data; minimal message content |
| LLM makes something up | LLM only rewrites tool outputs; numbers come from code |
| Facility upgraded to Level 2 | Reset its baseline at the upgrade date |
| Few states report vaccine stock | Do not penalise non-participating facilities; state the coverage limit |

## 12. How this scores against the rubric

| Criterion (weight) | What to show in the write-up and video | Risk and fix |
|---|---|---|
| Problem relevance and clarity (25%) | One sharp job ("which facilities to check first, and why"), the NHFS evidence, named users (LGA officers, State PHC Board), who is affected | Broad scope: present one job; map, assistant and WhatsApp are supporting parts |
| Technical execution (25%) | Runs on labelled synthetic data; precision@k against the baseline; stress test; fairness check; edge cases handled | Synthetic data: label it clearly and report the `c` threshold |
| Impact potential (25%) | A pilot plan: one LGA, four weeks, with the State PHC Board; measurable goals; a link to real decisions (e.g. choosing facilities for BHCPF support) | Overlap with existing dashboards: position as the triage layer that consumes them |
| Presentation and clarity (15%) | One story: pick an LGA, see the list, open a facility, record a result, see the list update | Complexity: demo only the Tier 1 path |
| Creativity (10%) | Silence types; ranking by people affected; the feedback loop that creates labels; the honest threshold test | Avoid over-claiming novelty |

## 13. Success measures

- Share of the top-20 flags that are correct (labelled synthetic in the demo)
- Improvement over the baseline "no report for 2 months"
- Pilot: days between a facility going quiet and someone checking in
- Pilot: share of flagged facilities confirmed as needing support
- Pilot: officer time to produce the weekly visit plan

## 14. Honest limits and what is still unverified

**Limits to state in the write-up**

- Medicine stock is mostly not in DHIS2, so it is inferred from service counts and other signals.
- Silence is not proof of a crisis; it is one input among several.
- Population served is a modelled estimate ("about 9,000"), and people often bypass their nearest PHC.
- Results come from synthetic data; the models may learn simulator assumptions, which is why stress tests exist.
- The simulated feedback loop shows the mechanism, not real officer behaviour.

**Do not state as fact until checked**

- Whether BHCPF payments depend on DHIS2 reporting (not confirmed)
- The number and role of financial oversight officers (sources say about 752 or 774)
- Contents and downloadability of the NPHCDA PHC Functionality Dashboard and the vaccine stock dashboard
- GRID3 and Health Facility Registry column names; whether NHFS 2023 and SDI facility-level files can be downloaded
- Whether NHLMIS stock data can be accessed
- NHFS figures (taken from news reports of the NBS report)
- The Sierra Leone (19%) and Tanzania (78% to 26% in 21 weeks) claims from the Pulse document
- All simulator parameters marked "Assumed"
- The official deadline and build-window dates

## 15. Pitch lines

- **Problem:** "Facilities don't report reliably, so officers are blind until a patient finds the shelf empty."
- **Solution:** "We read the data the system already has, turn late and missing reports into warning signs, rank who to visit first, and say why."
- **DHIS2:** "We sit on top of DHIS2. We don't replace it, and we don't write into it."
- **Facilities:** "They need to do nothing new."

## 16. Team roles

| Role | Owns |
|---|---|
| Data and simulator | Real geography, the fake world, hidden truth, DHIS2-shaped output |
| ML | Models, calibration, baselines, stress test, fairness check |
| App and dashboard | Officer list, facility page, status control, printable export |
| Research, story and submission | Problem framing, write-up, video, mock-ups, form |

Everyone must be able to explain what was built and why (a brief requirement).

## 17. Submission checklist (the one Google Form)

- [ ] Team name, and Health selected as the track
- [ ] One-page write-up as a Google Doc or PDF, with viewer access on (problem and why, approach and data, what works, what is placeholder, what is next)
- [ ] Demo video, 5 minutes at most, uploaded unlisted to YouTube
- [ ] Code, repo or dashboard link that opens without login
- [ ] Data sources used, marking real, public and synthetic
- [ ] Submitted well before the deadline (the form closes automatically; late entries are not scored)
