"""CLI: python -m laplace.evals [--cases DIR] [--provider mock|openai] [--runs N]

Vi du:
    python -m laplace.evals                          # offline, MockLLM script
    python -m laplace.evals --provider openai --runs 3   # so lieu that (can key)
    python -m laplace.evals --strategy plan_execute --provider openai
"""

import argparse
import json

from laplace.evals.harness import load_cases, run_suite


def main() -> None:
    parser = argparse.ArgumentParser(description="Laplace eval harness")
    parser.add_argument("--cases", default="evals/cases", help="file .yaml hoac thu muc case")
    parser.add_argument("--provider", default="mock", choices=["mock", "openai", "gemini"])
    parser.add_argument("--runs", type=int, default=1, help="so lan chay moi case")
    parser.add_argument("--out", default="eval_results")
    parser.add_argument(
        "--judge", default=None, choices=["mock", "openai", "gemini"],
        help="provider cham LLM-as-judge cho case co field `judge` (mac dinh: tat; "
        "nen chon model khac model agent de tranh thien vi)",
    )
    parser.add_argument(
        "--strategy", default=None, choices=[None, "react", "plan_execute"],
        help="ep tat ca case chay 1 chien luoc (chi dung voi --provider openai; "
        "mock_script gan voi chien luoc goc cua case)",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.strategy:
        if args.provider == "mock":
            parser.error("--strategy chi dung voi provider LLM that (mock_script "
                         "duoc viet rieng cho chien luoc goc cua tung case)")
        for c in cases:
            c.strategy = args.strategy
    print(f"Nap {len(cases)} case tu {args.cases}; provider={args.provider}, runs={args.runs}")
    summary = run_suite(cases, provider=args.provider, runs=args.runs, out_dir=args.out,
                        judge_provider=args.judge)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nBao cao: {summary['out_dir']}/report.md")


if __name__ == "__main__":
    main()
