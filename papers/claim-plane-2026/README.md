# Claim Plane: Enforceable Change Intents and Dynamic Scope for Parallel Coding Agents

Research metadata for the 2026 Claim Plane preprint.

## Publication

- Author: Maxim Nikolaev
- Submitted: 24 July 2026
- Format: preprint, 10 pages, 2 figures
- Subject: Software Engineering (`cs.SE`)
- Cite as: `arXiv:2607.21909 [cs.SE]`
- Version: `arXiv:2607.21909v1 [cs.SE]`
- Abstract page: https://arxiv.org/abs/2607.21909
- PDF: https://arxiv.org/pdf/2607.21909
- DOI: https://doi.org/10.48550/arXiv.2607.21909

The repository does not duplicate the paper PDF. arXiv is the archival source
for the publication, while this directory records citation metadata and the
connection to executable research artifacts.

## Reproduction

The published six-pair CooperBench mechanism check is implemented at:

```text
experiments/cooperbench/paper_6pair/
```

The study uses six frozen feature pairs, coder seed 101, and four execution
arms. The research runner validates the frozen CooperBench inputs and performs
a gold sanity stage before paid model calls.

See [`experiments/cooperbench/README.md`](../../experiments/cooperbench/README.md)
for the complete research workflow and Docker reproduction instructions.

## Citation

BibTeX is available in [`citation.bib`](citation.bib). The entry uses `@misc`
because the current archival record is a preprint rather than a journal article.

Software citation metadata for the repository is maintained separately in the
root [`CITATION.cff`](../../CITATION.cff).
