# Reproducibility Checklist

## Inputs

- Data files are named and versioned.
- Source URLs and download dates are recorded.
- Exclusion rules are explicit.
- Preprocessing steps are scripted or precisely documented.
- Any manual labels are accompanied by annotation rules.

## Code

- Scripts needed for the result are named.
- Entry command is documented.
- Random seeds are fixed where applicable.
- Train/test or analysis splits are stored.
- Generated outputs are not mixed with source data.

## Environment

- Runtime version is recorded.
- Package versions or lockfiles are recorded.
- Hardware, compute, or instrument requirements are recorded when relevant.
- External API/model versions are recorded.

## Outputs

- Expected output files are listed.
- Numeric tolerances are stated.
- Figures or tables trace back to exact scripts.
- Known non-determinism is disclosed.

## Report

- Matched results are separated from failed checks.
- Missing information is listed.
- Deviations from the original procedure are explicit.
