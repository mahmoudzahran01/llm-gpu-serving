# Evidence

Raw output of the commands behind the claims in `../README.md`, captured by the `scripts/demo-2x-*.sh` scripts on 2026-11-02/03.
Numbering follows the README's experiment numbers (20 to 32).

Editing applied before publication, and nothing else:

* the kind cluster is called `lab` here (it carried a different name during the run), so contexts and node names read
  `kind-lab`, `lab-worker2`, ...;
* the Unix user name and the home-directory paths of the llama.cpp binaries were replaced by neutral ones;
* the cluster also hosted an unrelated RAG demo in namespace `rag`, which is not part of this repository; it shows up in
  `32-teardown.txt` as proof that nothing else on the cluster was touched.

Timestamps, IPs inside the kind network (10.244.x.x pods, 10.96.x.x services), pod hashes and timings are as captured.
