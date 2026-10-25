"""Immutable reviewed report identities for historical record verification.

Keep old entries when adding a new report. This checks reviewed bytes, not source
commit authentication, present-state recovery feasibility or model prose.
"""
from .starship_replanning import digest

REVIEWED_REPORTS = {'cebf9a5aa0e0fd8723f4d4ce12a5eec10bf001c95a0e9d4123fe09df6eb06d67': {'docs/assets/starship-catch-guidance-search-20261013/summary.json': '23effc90c5d2afe48f3a922abe3dae1c3f97c9a1c934709221ae5f43cd1c545d', 'docs/assets/starship-catch-guidance-search-20261013/continuation.json': 'fbaf958652aa7c704a74d25c4f92f38c0055ca1c4cf6df10b880319e06e33b62', 'docs/assets/starship-m3-final-20261012/summary.json': '3d7549c2e82214f00a81b3bf2e9ec408f5f4d25bd1939218c75c3659d166880f', 'docs/assets/starship-catch-intercept-20261013/summary.json': '09e2a2d2264f994d00eed08bd9ae97340204ddd908fefb8e2f22c22b63076164', 'docs/assets/starship-catch-approach-diagnosis-20261014/summary.json': 'f2ee681d90d8b02648b459a19068dca20ac9f9817d2ca8c35ffb6d96e539cd7a'}}


def report_sources(report):
    identity = digest(report)
    if identity not in REVIEWED_REPORTS:
        raise ValueError("unreviewed_booster_recovery_report")
    return dict(REVIEWED_REPORTS[identity])
