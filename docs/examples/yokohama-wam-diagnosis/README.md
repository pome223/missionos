# ANWM conditioning diagnosis

Six frozen, offline L4 cases localized the Yokohama hold-view failure to a strong interaction between projected-image gaps and time conditioning. The original forecast PNG reproduced byte for byte. VAE-only reconstruction preserved the city image (RGB MAE about 0.94). Replacing the projection with the complete observed image reduced hold-view luminance MAE from 52.22 to 28.72 at time 4, and to 5.91 at time 0.

**The time-4 image still invents water despite passing the numeric image bounds.** Those bounds alone are insufficient admission evidence. The time-0 case is a static identity diagnostic, not qualified future prediction or movement. No flight code, thresholds or earlier failed result were changed. No new flight or VLA invocation occurred.

[Japanese report](REPORT-ja.md) · [Image comparison](index.html) · [Frozen protocol](protocol.json) · [All results](summary.json) · [Attribution](ATTRIBUTION.md).

The official-reference cases repeat one preselected dataset image sixteen times; they are not official trajectory evaluation or a held-out benchmark. Full-image substitution also changes small differences within projected known pixels, so black gaps alone have not been proven the unique cause.

Run `python docs/examples/yokohama-wam-diagnosis/reproduce/verify_bundle.py` to recompute the public raster metrics without GPU. Private raw histories and complete cloud logs are retained separately. Additional estimated cost: $0.4149; cumulative $10.4434 of the authorized $13. VM and boot disk absence confirmed; invoice unconfirmed.
