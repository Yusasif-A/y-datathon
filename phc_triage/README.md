# PHC Triage — backend + dashboard

Implements the solution doc: a synthetic-DHIS2 simulator, three risk models
(expected-report gap, silence-type classifier, calibrated trouble forecast),
priority ranking, and a dashboard (officer list / facility page / state
overview) with the Confirmed / Cleared / Just late feedback loop.

Everything here is **synthetic data**, clearly labelled. No DHIS2 connection
exists in this build — by design, per the doc's "we read, we never write"
rule for DHIS2.

## Spec-built pipeline (Simulator Specification v0.2)

The dataset and models are rebuilt to follow `PHC_Triage_Simulator_Spec`,
on **real facility locations**. When `models/bundle.joblib` and
`data/baseline/` exist, the backend serves this pipeline; otherwise it falls
back to the original `stimulate-data` path described further down.

```bash
cd phc_triage
pip install -r backend/requirements.txt
python generate_all.py     # 15 worlds: baseline, 8 stress scenarios, c sweep (~6 min, ~520 MB, git-ignored)
python -m ml.train         # train + full evaluation -> models/, reports/validation.md (~15 min)
cd backend && python main.py   # dashboard on http://localhost:8010
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
- Lead times 1–2 months, facility-level supply reliability, and a higher
  vaccine reorder point (spec placeholders produced unrealistic stockouts).

## Run it (original stimulate-data pipeline)

```bash
cd phc_triage/backend
pip install -r requirements.txt
python main.py        # first boot trains the models (~1 min), then caches to data/
```

Open http://localhost:8010/ — the backend also serves the dashboard
(`phc_triage/dashboard/`) as static files, so there's nothing else to start.

To force a clean retrain (new simulated world): delete `backend/data/*.joblib`
and `validation.json`, or POST to `/api/admin/retrain`.

## Layout

- `backend/simulator.py` — builds the fake state: facilities, 24 months of
  hidden truth (stock, staffing, fridges, closures), and the messy DHIS2-style
  reports that truth produces.
- `backend/features.py` — turns a world into a leak-free feature table
  (only past months ever feed a given month's features) plus the hidden
  labels used only for training/grading.
- `backend/train.py` — trains the three models and the validation numbers
  (precision@k vs the "silent 2 months" baseline, calibration, a stress test
  on a different fake world, rural vs urban fairness).
- `backend/scoring.py` — turns trained models + a world into the ranked,
  explained risk table the dashboard reads (priority = trouble chance x
  people affected, SHAP-grouped reasons, plain-language sentence, suggested
  action).
- `backend/main.py` — the FastAPI app: officer list, facility detail, state
  overview, feedback loop, CSV/printable export, weekly brief text.
- `dashboard/` — the three views from section 7 of the doc, plain HTML/CSS/JS,
  no build step.

## What's real vs mocked here

Built: simulator, all three models, ranking, officer list, facility page,
state overview, Confirmed/Cleared/Just late + mark visited + override, CSV
export, printable list, weekly brief (template), validation numbers.

Not built (per the doc, mock-up only): WhatsApp delivery, the facility
in-charge view, live SMS confirmation. `/api/weekly_brief` returns the exact
text a WhatsApp message would carry — wiring an actual WhatsApp Business
account to send it is the only missing piece for that path.
