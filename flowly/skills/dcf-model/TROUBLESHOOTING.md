# DCF Model Troubleshooting

**Reach for this guide when** recalc reports errors, the valuation comes out looking wrong, or flipping the case selector doesn't change anything.

## The model shows error values

### `#REF!`
- Almost always a formula pointing at a row that moved after a header was inserted.
- Fix: rebuild the affected formulas with the right row references, or restart following the layout-first sequence.
- Prevent: fix every row position before you write a single formula.

### `#DIV/0!`
- Something is dividing by zero or by an empty cell.
- Fix: guard the division, e.g. `=IF([divisor]=0,0,[numerator]/[divisor])`.

### `#VALUE!`
- A calculation is hitting text where it expects a number.
- Fix: confirm the inputs feeding the formula are genuinely numeric.

## The valuation looks off

### Implied price is far too high
- Check that terminal value isn't above ~80% of EV.
- Confirm terminal growth is below WACC.
- Re-examine whether the growth rates are achievable.
- Ask whether the margins are too generous.

### Implied price is far too low
- Recheck the net debt vs. net cash sign — getting this backwards swings the answer.
- See whether WACC is too high.
- Consider whether the projections are overly cautious.
- Consider whether terminal growth is set too low.

## The case selector does nothing

### Consolidation column won't update when you switch scenarios
- Confirm the selector cell actually holds 1, 2, or 3.
- Check that the INDEX/OFFSET formulas reference the correct row range and the selector cell.
- Make sure the selector reference is absolute (`$B$6`).
- Test it: change the selector by hand and watch whether the projection values move.
