# PHC Triage: Merged Solution Document

YoungDataPro Datathon 2026 · Health Track · Submission due Sat 11 Oct, 11:59 PM WAT

*Core = Triage (reads existing reporting data and ranks who to visit first). Borrowed from Pulse = optional urgent flag, facility-side view, WhatsApp notifications. Sample/synthetic data is always labelled.*

## 1. The problem in plain words

Nigeria has about 31,000 primary health centres (PHCs). They are most people's first contact with the health system. Facilities are supposed to report upward (facility → LGA → state → national system, DHIS2), but reports arrive late, incomplete or not at all. So officers and planners are guessing. A medicine runs out, and nobody above the facility knows until a patient arrives and there is none.

An LGA officer looks after roughly 40 facilities and can visit only a few each week.

**The question our tool answers:** *"Which facilities should I check first this week, and why?"*

## 2. The solution in one paragraph

The tool reads the data the system already collects (DHIS2-style monthly reports), treats late, falling and missing reports as warning signs, estimates which facilities are likely to run short of medicines, vaccines, staff or critical supplies in the next 1 to 2 months, multiplies that by how many people would be hurt, and shows the officer a short ranked list. Every flag has a plain-language reason and a suggested action. WhatsApp delivers the weekly list so officers do not need to log in to find out.

**One line for judges:** *"Facilities already report into DHIS2, but unreliably. We read that data, treat late and missing reports as warning signs, and tell officers which facilities to help first. We add a layer on top of DHIS2; we do not replace it."*

## 3. Relationship to DHIS2 (the question judges will ask)

| Question | Answer |
| --- | --- |
| Do we replace DHIS2? | No. |
| Do we write data back into DHIS2? | No. DHIS2 is a read-only source. |
| Where does our output go? | To our own dashboard and to officers' WhatsApp. Never into DHIS2. |
| Do facilities have to do anything new? | No, in the core tool. |
| What about the hackathon demo? | Real facility-level DHIS2 data is not public, so we use synthetic data shaped like DHIS2 (data element, facility, month, value), clearly labelled. Real data can replace it later. |

## 4. The whole flow, step by step

**Today (the problem):**

1. The health centre in-charge fills a paper monthly form (visits, births, vaccinations).
2. It goes to the LGA, where a records officer types it into DHIS2.
3. State and national levels see it in DHIS2.
4. Where it breaks: forms are late or missing, DHIS2 mostly holds service counts (not medicine stock), and the officer cannot tell which of 40 facilities to visit first.

**With our tool:**

```
Facility → paper form → LGA types into DHIS2 → (DHIS2-style data)
                                                    ↓
                                            OUR TOOL reads it
                                 (expected-report check, silence type,
                                  trouble forecast, people affected)
                                                    ↓
                              Ranked list with reasons + suggested action
                                                    ↓
                      Our dashboard  ←→  WhatsApp weekly brief to officer
                                                    ↓
                  Officer calls/visits, then marks Confirmed / Cleared / Just late
                                                    ↓
                          Tool learns from the answer (feedback loop)
```

**One facility's story:** Ada's health centre reported every month for 24 months, then went silent for 2. Only her facility is silent (not a whole-LGA typing backlog). She serves about 9,000 people, has unreliable power, fewer nurses than the Level 2 standard, and the next facility is 18 km away. Result: she is near the top of the list with the reason *"reported every month for 2 years, silent for 2, unreliable power, nearest alternative 18 km"* and the action *"call the in-charge, then verify through the ward committee."* The officer calls, finds the vaccine fridge has failed, and marks it Confirmed.

## 5. How the problem statement is covered

| Part of the brief | What the tool looks at | What it does |
| --- | --- | --- |
| Highest-risk facilities | All signals combined into one score | Ranked list sized to the officer's weekly capacity |
| Medicines | Drops in stock-dependent services (e.g. family planning), stock reports where they exist, distance, funding status | Chance that key medicines are short (stock is mostly inferred; we say so) |
| Vaccines | Immunisation counts, vaccine stock and discards, fridge status and power | Vaccine and cold-chain risk as its own failure type |
| Staff | Staff vs Level 1/2 standard, sudden volume drops with late reports, insecurity | Chance of understaffing or closure |
| Critical supplies | Power, water, equipment, connectivity | Adds infrastructure risk |
| Before it becomes a crisis | Reports drifting later, falling volumes, incomplete forms, plus a 1 to 2 month forecast | Flags earlier than "no report in two months" |
| People who rely on them | Population served, services at stake, distance to next facility | Ranks by chance of trouble × people affected |
| The reporting blind spot | Silence patterns and whole-LGA clusters | Separates one troubled facility from an LGA-wide data-entry backlog |

Brief's three suggested approaches: risk-scoring model (yes), geographic dashboard of under-resourced LGAs (yes, State overview), anomaly detection where reporting gaps are the warning sign (yes, silence-type model).

## 6. How it works behind the scenes

**Data (three kinds):**

- **Real:** facility locations and boundaries (GRID3, Health Facility Registry); population grids (WorldPop/GRID3); World Bank Service Delivery Indicators 2013-14 used to anchor how likely a facility is to be short of medicines given its traits; NHFS 2023 readiness figures where accessible.
- **Real but hard to get:** facility-level DHIS2 reports, NHLMIS stock records. Described as the pilot-stage plan.
- **Synthetic:** monthly reports and 4 to 5 tracer stock items (an antimalarial, a maternal drug, a family-planning commodity, a vaccine, oral rehydration) generated by a simulator. Always labelled.

**Three models:**

1. **Expected-report model:** given the facility's history, season and similar facilities, what should it have reported? A big gap is a warning sign.
2. **Silence-type model:** sorts a silence into late data entry, always a poor reporter, sudden drop, or whole LGA quiet. Each needs a different action.
3. **Trouble forecast:** chance of running short of medicines, vaccines or staff in the next 1 to 2 months.

**Ranking:** priority = chance of trouble × people affected. Show the top k, where k is the officer's weekly capacity.

**Explanations:** each flag gets one plain sentence and a suggested action.

**Feedback loop:** officers mark Confirmed / Cleared / Just late. Over time this creates the real answers that public data lacks.

**Simulator (how we test without real data):**

1. Create facilities with real locations and traits (rural/urban, power, staff, connectivity, size, insecurity).
2. Play out a hidden truth month by month (stock in and out, late funding, broken fridge, staff leaving, closures).
3. Turn truth into messy reports (struggling facilities report late, incompletely, or not at all; whole LGAs sometimes go quiet).

The model never sees the hidden truth when predicting. We run weak, medium and strong links between trouble and reporting problems, and report how strong the link must be for the tool to beat a simple baseline ("no report for 2 months").

**Validation to report:** precision@k (of the top k flagged, how many really had a problem), calibration, comparison to the simple baseline, a stress test on a different fake world, and a rural vs urban fairness check. Every number is labelled "synthetic data".

**Fairness rules (from both documents):**

- A missed report raises a "check in with them" flag, never a blame label.
- Thin or missing data never lowers a facility's rank.
- A rural facility is not flagged just for being rural or poorly connected; connectivity is a model input and each facility is compared with its own history first.
- Scores mean "review priority", not a verdict. Officers can override.

**Role of AI assistant (LLM):** numbers always come from code, never from the model. It writes the weekly brief, answers questions by turning them into fixed filters over the risk table (read-only), and drafts verification messages. A human approves everything before it is sent. The app works without the LLM using fixed templates.

## 6A. The models in detail

There are three models, a simple baseline to beat, and a ranking step. All three models see only what a real system would see (reports and facility traits). They are trained and graded on a hidden truth from the simulator that they never see as input.

### Model 1: Expected-report model

- **Question it answers:** given this facility's history, the season and similar facilities, what should it have reported this month?
- **Inputs:** past monthly reports (counts, lateness, completeness), season, facility traits (type, size, power, staff, connectivity, records officer present), LGA and state.
- **Method:** gradient-boosted model (for example LightGBM or XGBoost), predicting the expected volume and on-time reporting for each facility-month.
- **Output:** the gap between expected and actual. A big gap is a warning sign. Each facility is compared with its own history first and similar facilities second.
- **Why it matters:** it turns "late" or "low" into something measurable, so the tool can warn before the facility goes fully silent.

### Model 2: Silence-type model

- **Question it answers:** why is this facility quiet, and what should the officer do?
- **Classes:** late data entry, always a poor reporter, sudden drop, whole LGA quiet.
- **Inputs:** the facility's reporting pattern over time, what its neighbours in the same LGA are doing, connectivity and records-officer traits, and whether the silence started suddenly.
- **Method:** a classifier for the cause of each silence, plus a spatio-temporal scan that detects clusters of facilities going quiet together in one place and time.
- **Output and action:**

| Silence type | What the officer is told |
| --- | --- |
| Late data entry | Chase the paperwork; not urgent |
| Always a poor reporter | Low priority unless other signs appear |
| Sudden drop | Call the in-charge soon; may be a real problem |
| Whole LGA quiet | Send to the LGA data office, not 12 separate visits |
| Silence in an insecure area | Escalate; never suggest a visit |

### Model 3: Trouble forecast

- **Question it answers:** what is the chance this facility runs short of medicines, vaccines or staff in the next 1 to 2 months?
- **Inputs:** outputs of models 1 and 2, service counts that depend on stock (for example family planning, immunisation), structural traits (power, fridge, staff vs standard, distance, funding status), and the facility's recent trend.
- **Method:** gradient-boosted risk forecaster with calibrated probabilities (so "70%" really means about 70%) and an uncertainty estimate (conformal prediction or an ensemble).
- **Output:** a probability for each failure type: medicine, vaccine or cold chain, staff, supplies or infrastructure.
- **Honest note:** medicine stock is mostly not in DHIS2, so stock trouble is inferred from service counts, reporting behaviour and structural traits. The tool says so.

### Ranking step (not a model)

**Priority = chance of trouble × people affected.** People affected uses the catchment population (WorldPop and GRID3, shown rounded as "about 9,000"), the services at stake, and the distance to the next facility. The officer sees the top k, where k is the number of visits they can make.

### Explanations

SHAP shows which inputs pushed a facility's score up or down. The top reasons are turned into one plain sentence and a suggested action. Thin or missing data never lowers a facility's rank.

### Baseline to beat

The simple rule "no report for 2 months". If the models cannot beat it, they add nothing. We report how strong the link between trouble and reporting problems must be for the tool to beat it.

### Inputs vs answers (avoiding leakage)

| Item | Source | Role |
| --- | --- | --- |
| Monthly reports (counts, lateness, completeness), facility traits | Simulated | Inputs (features) for all three models |
| True stock, closures, failures in the next 1 to 2 months, the real cause of each silence | Simulated hidden truth | Answers (labels) for training and grading; never inputs |
| The simulator's report-probability formula | Inside the simulator | Never given to the models |
| Predicted probabilities and silence types | Model output | Feed the ranking |

### Training and testing

- Train on earlier months, test on later ones.
- Hold out whole LGAs and whole alternative fake worlds for stress tests.
- Stress test: train on one fake world, test on another with a weaker link, noisier data and more chronic non-reporters.
- Metrics: precision@k, recall@k, detection lag (how many months earlier than the baseline), calibration, and results split by rural vs urban and by zone.
- Every number is labelled "synthetic data".

### How the simulator makes the fake world

- **Stock:** stock(t+1) = stock(t) + received − consumed − expired. Demand comes from catchment, season and utilisation. Reordering has a delay and a fill probability based on supply-chain quality. Shocks include late funding, closures, power cuts, fridge failure, staff loss and supply waves across several facilities.
- **Reporting:** P(on-time) = logistic(base + records officer + connectivity − workload + LGA effect + state effect + facility habit − c × distress). The link c between trouble and reporting problems is unknown, so we run weak, medium and strong values.
- **Data shape:** a long table (data element, period, facility, category, value) plus facility metadata, matching DHIS2 so real data can replace it. Hidden-truth tables are separate and the models never read them.

### Real-data plan

On real data the simulator labels are replaced by real ones: officer Confirmed / Cleared / Just late answers first, and real stock records later if access is granted. The models are then retrained.

### Fairness checks

Connectivity and records-officer presence are model inputs, so a rural facility with a bad network is not flagged just for reporting late. Results are tested separately for rural and urban facilities.

## 7. The interface: what each screen should look like

One web app, three main views plus an assistant panel and a printout. All views read the same scores, so a status marked in one updates all others. Mobile-friendly. Every sample value tagged **"Sample data"**.

### 7.1 Officer list (home screen)

**For:** LGA coordinators and M&E officers.

**Top bar:** LGA name · "Data as of \[date\]" label · "Offline copy" label · Print button.

**Three number cards:** Facilities in LGA · Flagged this week · LGA-wide alerts.

**Warning banner (only when needed):** e.g. "11 facilities went quiet together in March. Likely a data-entry backlog. Contact the LGA data office."

**Trip input:** "How many visits can you make this week?" \[number box\] → the list is cut to that many.

**Ranked cards, one per facility:**

| Element | Example |
| --- | --- |
| Rank | 1 |
| Facility name | Ada's Health Centre |
| Tag | HIGH (red) / MEDIUM (amber) / LOW (green) |
| People served | About 9,000 |
| Reason (one sentence) | Reported every month for 2 years, silent for 2. Unreliable power. Nearest alternative 18 km. |
| Suggested action | Call the in-charge, then verify through the ward committee |
| Buttons | **Confirmed · Cleared · Just late** |

**Extras:** Export to CSV · one-page printable list · visit order that balances priority against distance.

### 7.2 Facility page

**For:** anyone who opens a facility from the list.

- **Header:** facility name, priority tag, people served, type, location.
- **Reporting chart:** monthly bars of reports received, a dashed line for the expected level, missing months outlined.
- **"Why it is flagged" panel:** horizontal bars showing how much each reason pushed the score up (e.g. silence, power, staff gap, distance).
- **Small tiles:** Staff · Cold chain/fridge · Connectivity · Last supply delivery · Days since last report.
- **Silence type:** late entry / chronic poor reporter / sudden drop / LGA-wide.
- **Suggested action** and the same three buttons (Confirmed, Cleared, Just late) plus "Mark visited" and "Override flag".

### 7.3 State overview

**For:** State PHC Board and planners.

- **Number cards:** total PHCs · flagged High / Medium / Low.
- **LGA tiles** coloured by priority with the number flagged (the real version uses a map built from GRID3 boundaries). Tapping a tile opens that LGA's officer list.
- **Cluster alerts list:** e.g. "11 facilities quiet in March, likely data-entry backlog" or "quiet after an insecurity report: escalate, do not send visits".
- **LGAs ranked** by number of flagged facilities, with a way to compare LGAs.

### 7.4 Assistant panel (nice to have)

- **Weekly brief:** a short paragraph generated from the ranked list, ready to copy into WhatsApp or meeting notes. *Build this first.*
- **Question box:** "Which facilities in Ward 7 have not reported?" Answered from fixed filters over the risk table.
- **Draft verification message:** fixed wording with the facility name filled in; the officer reviews and edits before sending.

### 7.5 Printable list

One page: LGA name, date, top-k facilities with reason and action, blank boxes to tick Confirmed / Cleared. For officers with no connection.

### 7.6 Facility in-charge view (optional, from Pulse; show as mock-up)

Only shown as a next step, because the core tool needs nothing from facilities.

- **My facility:** its own flag status, reporting history (proof of what it reported), any planned support visit.
- **Urgent need button:** a critical item running out jumps to the top of the officer's list.
- **Optional short check-in** (about 10 tracer items in stock / low / out, staff on duty, beds), pre-filled with last values.

## 8. WhatsApp: what it is and where the link goes

**WhatsApp is a delivery channel only. It is not embedded in the dashboard, and it does not connect to DHIS2.**

```
Our tool (scores risk, writes message text)
        ↓
WhatsApp Business API (Meta, or a provider such as Twilio)
        ↓
Phone of the officer or facility in-charge
        ↓ (optional replies)
WhatsApp sends reply back to our backend → saved in OUR database → dashboard updates
```

**Where does the link in a WhatsApp message go?** To **our own dashboard** (for example the officer's LGA list, or a facility's page). It never goes to DHIS2. DHIS2 is only a read-only data source behind the scenes.

**Messages:**

| To | Message | Purpose |
| --- | --- | --- |
| Officer (weekly) | "Weekly priority list, \[LGA\]. 1) Ada's Health Centre: HIGH. Silent 2 months, serves about 9,000 people. Suggested action: call the in-charge. Open full list: \[dashboard link\]" | Officer gets the answer without logging in. Build this first. |
| Facility in-charge (reminder) | "Hello Nurse Ada, we have not received your report for March. Please send it to your LGA records officer." | Helps fix late reporting at the source. Never blaming. |
| Facility in-charge (confirmation) | "Please confirm if your vaccine fridge is working. Reply YES or NO." Reply is saved to our database. | Verification the officer approved. |

**Rules:** an officer approves any message to a facility before it is sent; no patient data ever; no blame language.

**What is needed to make it real:** a WhatsApp Business account, pre-approved message templates, a small backend to send messages and receive replies, per-conversation cost. Setup takes days, so for the hackathon it is a mock-up.

**Honest caveat:** if facilities already report late, a new weekly check-in may also be ignored. That is why the core tool does not depend on it, and why the facility check-in is optional.

## 8A. How unreliable facilities are brought in

The tool never depends on facilities fixing their reporting. Silence is treated as a signal, and facility effort is added in steps.

| Level | What happens | Facility effort | Status |
| --- | --- | --- | --- |
| 1. Core | Silence is the warning; the officer follows up by call or visit and records the answer (Confirmed / Cleared / Just late) | None | Build now |
| 2. Next step | One-question WhatsApp reminder or YES/NO reply, e.g. "Is your vaccine fridge working? Reply YES or NO." Replies are saved to our database and shown to the officer | About 10 seconds, no login, no link | Mock-up |
| 3. Later | Optional short check-in (about 10 tracer items) plus the facility's own history page | About 2 minutes | Mock-up |

**Outside check:** for facilities that stay silent, a ward committee member or community health worker confirms by SMS or call whether the clinic is open or the drug is available.

**Why facilities would bother:** they see that reporting led to help (a planned visit, a thank-you on reply), and the officer follows up fast on urgent alerts.

**Phone numbers:** there is no public list. In a pilot they come from the LGA PHC coordinator and the State PHC Board, with each in-charge's consent (NDPA 2023). In the demo, use fake numbers labelled Sample data. Facility messages contain no dashboard link in the core tool; only officers and state planners open the dashboard.

## 9. What to build for the deadline vs describe as next steps

**Build / show:**

- Synthetic DHIS2-shaped data and simulator (clearly labelled)
- Risk scoring with reasons and suggested actions
- Officer list, facility page, state overview (the three views)
- Confirmed / Cleared / Just late buttons, mark visited, override, CSV export, printable list
- Weekly brief (LLM with fixed-template fallback)
- Validation numbers: precision@k, baseline comparison, stress test, fairness check (all labelled synthetic)

**Mock-up only (do not claim it is built):**

- WhatsApp messages to officer and facility
- Facility in-charge view, urgent button, optional check-in
- True offline caching, live SMS confirmation by ward committees

**Pilot plan (state as next steps):** one LGA for four weeks under a data-sharing agreement with the State PHC Board; connect to DHIS2 via API (aggregated facility-level data only); request vaccine stock extract through NPHCDA; officer confirm/clear answers become real labels; retrain the models on them.

## 10. Success measures

- Share of the top-20 flags that are correct (labelled synthetic in the demo)
- Improvement over the simple baseline ("no report for 2 months")
- Pilot: days between a facility going quiet and someone checking in
- Pilot: share of flagged facilities confirmed as needing support
- Pilot: officer time to produce the weekly visit plan

## 11. Honest limits to state

- Medicine stock is mostly not in DHIS2, so it is inferred from service counts and other signals.
- Silence is not proof of a crisis; it is one input among several.
- Population served is a modelled estimate (show "about 9,000"), and people often bypass their nearest PHC.
- Results are from synthetic data until real data is available; the tool may learn simulator assumptions, which is why stress tests exist.
- The SDI data is a 2013-14 snapshot and access terms are unchecked; no one has yet downloaded it.

## 12. Team roles

| Role | Owns |
| --- | --- |
| Data and simulator | Real geography, the fake world, hidden truth, DHIS2-shaped output |
| ML | Three models, calibration, baselines, stress tests, fairness check |
| App and dashboard | Officer list, facility page, state overview, buttons, printable export |
| Research, story and submission | Problem framing, one-page write-up, video, WhatsApp mock-ups |

Everyone must be able to explain what was built and why.

## 13. Pitch lines

- **Problem:** "Facilities don't report reliably, so officers are blind until a patient finds the shelf empty."
- **Solution:** "We read the data the system already has, turn late and missing reports into warning signs, rank who to visit first, and say why."
- **DHIS2:** "We sit on top of DHIS2. We don't replace it, and we don't write into it."
- **Facilities:** "They need to do nothing new. WhatsApp is how we get the weekly list to officers."

## 14. Build order (start here)

1. Simulator and synthetic DHIS2-shaped data (facility, data element, month, value), labelled Sample data.
2. Risk scoring: expected-report check, silence type, trouble forecast, priority = chance of trouble × people affected.
3. Baseline comparison and precision@k, so the numbers are ready early.
4. Officer list with reasons, suggested actions, trip input and the three buttons.
5. Facility page and state overview.
6. Weekly brief (template first, LLM second), CSV export, printable list.
7. WhatsApp and facility-view mock-ups, then the write-up and video.

Cut from the bottom if time runs short.
