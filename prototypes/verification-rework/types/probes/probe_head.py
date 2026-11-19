"""Mistakes written against TODAY's GrounderResult. Does the checker object?"""
from gct.grounder.answer import Citation, Coverage, GrounderError, GrounderResult, GrounderState, Integrity

c = Citation(label="S1", file="f.pdf", page_or_slide=1, chunk_id="x")
ok, cov = Integrity(ok=True, reasons=[]), Coverage(complete=True, gaps=[])
a = GrounderResult(state=GrounderState.ERROR, answer_prose=None, citations=[], coverage=cov, integrity=ok, error=None)  # ERROR, no error
b = GrounderResult(state=GrounderState.REFUSAL, answer_prose="prose", citations=[c], coverage=cov, integrity=ok, error=None)  # refusal WITH an answer
d = GrounderResult(state=GrounderState.GROUNDED, answer_prose=None, citations=[], coverage=cov, integrity=Integrity(ok=False, reasons=["x"]), error=GrounderError(kind="k", message="m"))  # grounded, flagged, errored
e = GrounderError(kind="provider_transiet", message="typo")  # misspelt kind


def correct_code(r: GrounderResult) -> str:
    if r.state is GrounderState.ERROR:
        return r.error.kind  # CORRECT code - flagged anyway?
    return "ok"
