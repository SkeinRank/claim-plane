# Claim Plane: Reliability Gains and the Limits of Selective Concurrency for Parallel Coding Agents

Research metadata for the 2026 Claim Plane confirmatory preprint.

## Publication

- Author: Maxim Nikolaev
- Submitted: 2 August 2026
- Format: preprint, 10 pages, 4 figures, 6 main-text tables
- Subject: Software Engineering (`cs.SE`)
- Cite as: `arXiv:2608.00947 [cs.SE]`
- Version: `arXiv:2608.00947v1 [cs.SE]`
- Abstract page: https://arxiv.org/abs/2608.00947
- PDF: https://arxiv.org/pdf/2608.00947
- DOI: https://doi.org/10.48550/arXiv.2608.00947

The repository does not duplicate the paper PDF. arXiv is the archival source for
the publication, while this directory records citation metadata and the connection
to executable research artifacts.

## Study and reproduction

The paper reports a confirmatory study on 30 frozen CooperBench feature pairs,
balanced between 15 conflict and 15 clean labels, with three coder seeds, four
coordination arms, and 360 completed executions.

The executable protocol is maintained at:

```text
experiments/cooperbench/confirmatory_30x3/
```

See [`experiments/cooperbench/README.md`](../../experiments/cooperbench/README.md)
for the full staged workflow, frozen-plan protocol, sharding, deterministic
aggregation, publication manifest, and Docker reproduction commands.

Public study artifacts are archived at:

- https://huggingface.co/datasets/skeinrank/claim-plane-confirmatory-30x3

## Citation

BibTeX is available in [`citation.bib`](citation.bib). The entry uses `@misc`
because the current archival record is a preprint rather than a journal article.

Software citation metadata for the repository is maintained separately in the root
[`CITATION.cff`](../../CITATION.cff).
