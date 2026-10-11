# PHC Triage — backend + dashboard

Idon-Lafia's engine: a synthetic-DHIS2 simulator, three risk models
(expected-report gap, silence-type classifier, calibrated trouble forecast),
priority ranking, a dashboard (officer list / facility page / state
overview) with the officer feedback loop, and urgent alerts to officers by
WhatsApp and BimpeAI phone call. See `../project-SUMMARY.md` for the overview.

Everything here is **synthetic data**, clearly labelled. No DHIS2 connection
exists in this build — by design, per the doc's "we read, we never write"
rule for DHIS2.

## Data and models

The dataset is simulated on **real facility locations**. The backend serves `data/baseline/` with the
trained models in `models/bundle.joblib`; both are committed, so the
dashboard runs without regenerating anything.

```bash
cd phc_triage
pip install -r backend/requirements.txt
python generate_all.py     # 15 worlds: baseline, 8 stress scenarios, c sweep (~6 min, ~520 MB, git-ignored)
python -m ml.train         # train + full evaluation -> models/, reports/validation.md (~15 min)
cd backend && python main.py   # dashboard on http://localhost:5005
```

| Piece | What it is |
|---|---|
| `inputs/` | Real inputs: 3,381 public primary facilities in Oyo, Kano, Gombe from **GRID3 NGA Health Facilities v3.0**, and calibration rates from **NMIS 2014**. Rebuild with `python -m simulator.prepare_inputs`. |
| `configs/` | One YAML per scenario. Every parameter is tagged Documented / Reported / Assumed / Interp / Real / NMIS. |
| `simulator/` | Layers A–D plus sanity checks (spec 5–8, 12). `run.py` runs one world; `--preview 50 12` writes a small sample to eyeball. |
| `data/<world>/` | Model-visible `facilities.csv`, `reports.csv` (DHIS2 long shape), `stock_reports.csv`; hidden `truth_*.csv`; `run_metadata.json` with seed, config hash and sanity checks; `feedback.csv` (baseline). |
| `ml/` | Leak-free features (only reports received by month t), the three models, evaluation and the simulated feedback loop (spec 10–11). |
| `reports/validation.md` | All results, labelled SYNTHETIC. |
| `preview_sample_SYNTHETIC.xlsx` | 51 facilities × 12 months, visible and hidden side by side. |

Decisions that depart from the spec (each is noted where it is set):

- **Real geography.** Facility locations, LGAs, wards, types and settlement
  types are real; names are neutral labels (`Ibarapa East PHC 014`) because
  the outcomes attached to them are invented. `grid3_id` traces each row.
- **Calibrated to NMIS 2014:** grid power (urban 65%, rural 18%), the
  power–fridge link, and the urban/rural staffing gap.
- **Tracer availability target 60–75%** instead of 30–40%. The NHFS 34% is
  across a long essential-drug list; NMIS shows ACT in stock at 77% of
  primary facilities.
- **Forecast target = serious trouble** (2+ medicines short, or vaccine short
  while the fridge is down, or staff below half, or closed). The spec's
  "any single item short" is true in ~72% of facility-months and cannot rank.
  All spec flags remain in `truth_facility_month.csv`.
- **Reporting:** 80% is the *received* rate (as DHIS2 counts it); on time is
  ~65%, close to the 64% timeliness reported for five states.
- **Insecure-area threshold 0.7** (spec 0.5 flagged 91% of Gombe as "never visit").
- **Sticky stockouts.** With the spec's placeholder supply settings, stock
  flickered in and out every month (an ACT stockout had no memory; spells
  lasted ~1.5 months), so trouble this month said nothing about next month.
  Now: reordering on stock + deliveries on the way, a reorder point that
  covers the 1–2 month lead time, and multi-month supply disruptions that hit
  weak and remote facilities more often. ACT stockouts now last ~3 months and
  continue month to month 70% of the time.

## Run it

```bash
cd phc_triage/backend
pip install -r requirements.txt
python main.py        # API + dashboard on http://localhost:5005/
```

The backend also serves the dashboard (`phc_triage/dashboard/`) as static
files, so there's nothing else to start. To expose it publicly and redeploy
the Firebase dashboard, run `start_tunnel.ps1` from the repo root.

WhatsApp and calls read the root `.env`: `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `PUBLIC_WHATSAPP_NUMBER`
and `BIMPEAI_API_KEY`. Without them the dashboard still works; sends report
"not configured".

## Backend layout

- `backend/main.py` — the FastAPI app: officer list, facility detail, state
  overview, feedback loop, CSV/printable export, weekly brief, alert and
  call endpoints, and the `/whatsapp` webhook.
- `backend/spec_pipeline.py` — scores the baseline world with the trained
  models into the ranked, explained risk table the dashboard reads.
- `backend/scoring.py` — shared wording: reasons, suggested actions, LGA
  cluster alerts.
- `backend/alerts.py` — automatic urgent alerts: who to notify, what is
  newly urgent, the send log.
- `backend/whatsapp.py` — WhatsApp Business Cloud API sender.
- `backend/calls.py` — BimpeAI phone calls that speak the alert.
- `dashboard/` — plain HTML/CSS/JS, no build step.
