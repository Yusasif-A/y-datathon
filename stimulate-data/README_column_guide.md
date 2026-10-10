# PHC Triage synthetic data: column guide (ALL SAMPLE DATA)

Every table has `world` (A or B) and `is_synthetic = True`.
World A: medium link between trouble and reporting problems (build and train here).
World B: weaker link, noisier, more chronic non-reporters (stress test: train on A, test on B).
10 LGAs, 311 facilities per world, 36 months (202310 to 202609), data as of 2026-10-09.

| File | Contents |
|---|---|
| 1_facilities_and_risk.csv | One row per facility: traits (LGA, ward, coordinates, type, rural/urban, population served, power, fridge, staff vs standard, connectivity 0-2, insecurity, distance to next facility, funding), fake in-charge phone, whatsapp_opt_in, PLUS the placeholder heuristic ranking (rank_in_state, flag, reason, suggested_action, people_affected, reporting signals) and officer_response (Confirmed / Cleared / Just late) for the top 30. The ranking is not the ML; it lets the dashboard be built first |
| 2_facility_month_reports.csv | Per facility-month: status (on_time / late / missing), days_late, completeness, service counts (OPD_TOTAL, ANC_1ST, DELIVERIES, FP_NEW, PENTA3, MALARIA_ACT), stock_ACT / OXY / DEPO / PENTA / ORS and days_out_ACT / OXY / DEPO / PENTA / ORS (days stocked out); blank unless the facility reports stock that month |
| 3_HIDDEN_TRUTH_labels_only.csv | Answer key. trouble_next_1_2m = label for the trouble forecast; silence_cause = label for the silence-type model (late_data_entry, chronic_poor_reporter, sudden_drop, lga_quiet, closure_or_insecurity); lga_quiet_event marks planted LGA-wide backlogs. NEVER use as model input |
| 4_state_overview.csv | Per LGA: facility counts, high / medium / low, people in high-flag facilities, cluster alert, fake LGA coordinator phone |
| WhatsApp_message_samples.txt | Weekly officer brief, facility reminder and YES/NO question for the mock-ups |

Rules: train on earlier months and test on later ones; train on world A, stress-test on world B; say in the write-up that all results are on synthetic data.
Sanity numbers (world A): 78% of reports arrive, trouble in 18% of facility-months. Placeholder heuristic got 35% of its top 20 right (base rate 20%, "no report" baseline 30%). These are not ML results.

## Key terms
- status: on_time, late (arrived 5 to 60 days late) or missing.
- trouble_now: facility closed, fridge failed, 2+ tracer items out, or understaffed rural facility with an item out.
- connectivity_score: 0 poor, 1 fair, 2 good.
- DHIS2 shape: file 2 holds the service counts as columns (one row per facility-month). To get DHIS2 long format (dataElement, period, orgUnit, value), melt the count columns.

## How the fake world was made (for the write-up)
1. 10 LGAs, facilities scattered around approximate LGA centres.
2. Hidden truth each month: tracer stock-outs (sticky, with LGA-wide supply waves), fridge failures, staff shortages, closures.
3. Reports come from facility traits and habit, and get worse as trouble approaches (drift starts about a month ahead). P(on-time) = logistic(base + records officer + connectivity - workload + LGA effect + facility habit - c x distress). Service counts fall when related stock is out.
4. Five planted LGA-wide quiet events (data-entry backlogs) in each world.
Regenerate with: python generate_phc_synthetic.py --world A --seed 42 --out ./phc_synthetic_data
