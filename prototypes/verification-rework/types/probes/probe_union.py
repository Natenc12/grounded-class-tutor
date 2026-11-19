"""The same mistakes against the tagged union."""
from typing_extensions import assert_never

from gct.grounder.answer import (Citation, Coverage, Failed, Grounded, GrounderError, GrounderResult,
                                 GrounderState, IntegrityFlagged, Partial, Refusal)

c = Citation(label="S1", file="f.pdf", page_or_slide=1, chunk_id="x")
cov = Coverage(complete=True, gaps=[])
a1 = Failed()  # ERROR, no error
a2 = Failed(error=None)
b = Refusal(coverage=cov, answer_prose="prose", citations=[c])  # refusal WITH an answer
d1 = Grounded(answer_prose=None, citations=[], coverage=cov)  # grounded without prose
d2 = Grounded(answer_prose="p", citations=[c], coverage=cov, error=GrounderError(kind="provider_terminal", message="m"))
e = GrounderError(kind="provider_transiet", message="typo")  # misspelt kind


def correct_code(r: GrounderResult) -> str:
    if r.state is GrounderState.ERROR:
        return r.error.kind  # correct - accepted?
    return "ok"


def forgot_the_check(r: GrounderResult) -> str:
    return r.error.kind  # WRONG - flagged?


def forgot_a_state(r: GrounderResult) -> str:
    match r:
        case Grounded() | Partial():
            return r.answer_prose
        case Refusal():
            return "refused"
        case Failed():
            return r.error.message
        case _:  # IntegrityFlagged was forgotten
            assert_never(r)
