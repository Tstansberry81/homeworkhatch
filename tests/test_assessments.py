"""The assessment classifier against ~300 synthetic cases (tests/classifier_cases)."""

import pytest

from app.services import assessments as A

from .classifier_cases.adv import ADV, GUARDS
from .classifier_cases.adv2 import ADV2
from .classifier_cases.blind2 import BLIND2
from .classifier_cases.cases import CASES
from .classifier_cases.holdout import HOLDOUT
from .classifier_cases.topics import TOPICS

# Known trade-offs: "Unit 3 Test - Absent Students Only" is a make-up sitting the planner folds into
# the main test, though the classifier still calls it high confidence; a review quiz in a group
# literally named "Review Quizzes" comes out "none", the price of not planning "Exam Review" groups.
KNOWN = {"HS test for absent students", "Review quiz in 'Review Quizzes' group"}


def _cases():
    for suite, cases in (("tuned", CASES), ("holdout", HOLDOUT), ("topics", TOPICS), ("blind2", BLIND2),
                         ("adversarial", ADV), ("adversarial2", ADV2), ("guards", GUARDS)):
        for c in cases:
            label, fields, expected = (c[0], c[2], c[3]) if len(c) == 4 else c
            yield pytest.param(fields, expected, id=f"{suite}:{label}",
                               marks=pytest.mark.xfail(strict=True) if label in KNOWN else ())


@pytest.mark.parametrize("fields,expected", list(_cases()))
def test_classifier(fields, expected):
    r = A.classify(A.Item(**fields))
    kind, _, confidence = expected.partition("/")
    assert r.kind == kind, (r.kind, r.confidence, r.reasons)
    if confidence:
        assert r.confidence == confidence, (r.kind, r.confidence, r.reasons)


def test_family_keys_keep_modifiers_and_fold_parts():
    assert A.stem("Quiz 6 (Extra Credit)") != A.stem("Quiz 5"), "one 'not a test' on extra credit doesn't hide every quiz"
    assert A.stem("Quiz 5") == A.stem("Quiz 12")
    assert A.stem("Exam 1 Part A", sibling=True) == A.stem("Exam 1 Part B", sibling=True)
