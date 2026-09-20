"""Human-readable, token-redacted scheduler cutover preview and confirmation."""

from __future__ import annotations

import argparse
import getpass

from investor_core.background_worker import CoreClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", default="http://127.0.0.1:8710")
    parser.add_argument("--target", choices=["WINDOWS", "HERMES", "PAUSED"], default="WINDOWS")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    client = CoreClient(args.core_url)
    try:
        preview = client.request("GET", f"/v1/background-scheduler/preview?target={args.target}")
        print(f"调度来源:{preview['current_source']} -> {preview['target_source']}")
        for policy in preview["policies"]:
            print(
                f"{policy['job_name']}: {policy['schedule']} "
                f"({policy['timezone']}), 启用={policy['enabled']}"
            )
        print("仅接管每日行情、每日风险、系统健康;其他 Hermes 能力保持。")
        print("切换前历史漏跑不自动补跑;补跑窗口 24 小时;通知投递尚未迁移。")
        print("不产生买卖,不更改持仓、现金或投资策略。")
        if not args.apply:
            print("预览完成,未创建草稿或切换来源。")
            return
        draft = client.request(
            "POST",
            "/v1/background-scheduler/drafts",
            {"target": args.target, "actor_ref": getpass.getuser()},
        )
        if input("确认上述调度来源变更?输入 确认 / 取消:").strip() != "确认":
            print("已取消,来源未变。")
            raise SystemExit(2)
        client.request(
            "POST",
            f"/v1/background-scheduler/drafts/{draft['draft']['id']}/commit",
            {"confirmation_token": draft["confirmation_token"], "confirmed_by": getpass.getuser()},
        )
        print(f"来源已切换为 {args.target}。尚需核对实际心跳与下次任务运行。")
    finally:
        client.client.close()


if __name__ == "__main__":
    main()
