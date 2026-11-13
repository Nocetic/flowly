# Statistical Test Selection

## Before Selecting A Test

- Define the primary outcome.
- Define the experimental unit.
- Identify independent, paired, repeated-measures, or clustered design.
- Record group sizes.
- Check whether the variable is continuous, ordinal, count, binary, categorical, or time-to-event.
- Decide which comparisons were planned before looking at results.

## Common Choices

| Situation | Common analysis |
| --- | --- |
| Two independent groups, continuous outcome | Welch t-test; Mann-Whitney as robustness check when distributions are badly non-normal. |
| Two paired measurements | Paired t-test; Wilcoxon signed-rank when paired differences are not suitable for parametric analysis. |
| More than two independent groups | ANOVA or Welch ANOVA; Kruskal-Wallis for rank-based comparison. |
| More than two repeated measurements | Repeated-measures ANOVA or mixed model. |
| Binary outcome by group | Logistic regression, chi-square, or Fisher exact test for small counts. |
| Count outcome | Poisson or negative binomial model, depending on dispersion. |
| Correlation between continuous variables | Pearson for linear relation, Spearman for monotonic/rank relation. |
| Prediction model performance | Predefined train/test split or cross-validation; report uncertainty across folds or bootstrap. |

## Red Flags

- Multiple rows per subject treated as independent.
- Selecting tests after seeing which p-value is smallest.
- Dropping outliers without a predefined rule.
- Using percent change when the baseline can be near zero.
- Reporting "no difference" from a non-significant p-value without power or interval discussion.
