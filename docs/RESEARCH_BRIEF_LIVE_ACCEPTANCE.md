# 主动研究入口：公开取数验收（2026-11-17）

## 1. 结论

云端实际完成公开取数、双渠道比较、历史统计及基金净值观察。当前本机Core不可达；没有读取真实持仓，没有生产部署。源代码功能与投资判断效果分别验收。用户无需补公开行情截图。

## 2. 范围

承接用户明确的补数据、补能力要求，完成按需公开研究入口。未扩大投资批准、业务配置或生产部署权限。

## 3. 实际结果

观察区间：2026-11-13收盘至2026-11-17收盘；读取日期：2026-11-17，Asia/Shanghai。

| 指数 | 区间变化 | 最新相邻观测 | 单次幅度历史百分位 | 区间幅度历史百分位 |
|---|---:|---:|---:|---:|
| 上证指数 | -1.37% | -0.50% | 47.2 | 76.6 |
| 深证成指 | -3.01% | -0.49% | 24.6 | 84.1 |
| 创业板指 | -5.52% | -1.20% | 44.4 | 92.1 |

样本各252个历史观测窗口，区间为两个观测间隔；多日窗口重叠。三个主要指数不代表全市场广度。双渠道数值匹配不表示上游独立核验。

| 公开关注产品 | 最新单位净值 | 净值日期 | 区间单位净值变化 |
|---|---:|---|---:|
| 000032 易方达信用债债券A | 1.1317 | 2026-11-17 | 0.02% |
| 002340 富国价值优势混合A | 2.9519 | 2026-11-17 | -1.69% |
| 022463 富国中证A500ETF发起式联接A | 1.037 | 2026-11-17 | -1.59% |
| 040046 华安纳斯达克100ETF联接(QDII)A | 7.4689 | 2026-11-16 | 末端缺失，未替换 |

此表只包含公开产品数据，不代表任何账户持仓；未处理分红，不是个人收益。

## 4. 已可运行入口

云端实际运行 `investor-assistant --json research-brief --start 2026-11-13 --end 2026-11-17 --fund 022463 --fund 000032 --fund 002340 --fund 040046`。Core连接失败后继续完成公开研究。入口说明见 RESEARCH_BRIEF.md；生产入口尚未新增此命令。

## 5. 交付状态

代码实现与公开数据运行已完成；46项研究与日常客户端专项测试、Ruff及相关mypy通过；本地全量回归800通过、5跳过；之后的局部加固另经专项验证。最新双平台CI尚未通过。源码合并、版本发布、本机部署、生产验收未完成。没有将软件测试等同于投资效果验证。

## 6. 尚未完成

真实持仓关联只能在可访问现有Core的环境验证；行业穿透、含分红收益、估值/盈利与策略效果仍需继续实现与验证。没有把缺项判定为安全或自动推迟到阶段外。

## 7. 数据与行为影响

公开研究请求仅GET。没有写Core、更新数据库、改变策略或财务事实。实际生产整体状态未知。数据是读取时点资料，不冒充过去的前瞻档案。

## 8. 下一步与授权

继续工程集成及真实数据分析；生产部署遵循独立授权，不以本次公开取数成功替代本机验收。当前阶段任务及检查点维护在原LAUNCH_CHECKLIST。

## 来源

- sh000001: https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001&klt=101&fqt=0&beg=19900101&end=20261117&lmt=640&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57；读取 2026-11-17T22:47:02.148830+08:00；SHA256 `717b14e292047cba8a518d65f8aabce69f75993d20fc8b454b335493776d8379`。
- sh000001: https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=640；读取 2026-11-17T22:47:10.000303+08:00；SHA256 `b95dc45d1db5ec9dfde7b552a4251c09ba9fc62f3c1d37b51ea36c6bb909a53c`。
- sz399001: https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.399001&klt=101&fqt=0&beg=19900101&end=20261117&lmt=640&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57；读取 2026-11-17T22:47:01.150609+08:00；SHA256 `a5303e6f97189985462f9c6834aa7d7b91d4b339dd17d35af87f436ad01fcfde`。
- sz399001: https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sz399001&scale=240&ma=no&datalen=640；读取 2026-11-17T22:47:09.976750+08:00；SHA256 `da3ecc72d200c3171af50d0ed98dac2e63f544a305c06b7e0258b9822919cfaf`。
- sz399006: https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.399006&klt=101&fqt=0&beg=19900101&end=20261117&lmt=640&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57；读取 2026-11-17T22:47:03.088507+08:00；SHA256 `adb0f0278bfadd1454b549e87d02d390d658703ac57485e7b5bd073e4a997aaa`。
- sz399006: https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sz399006&scale=240&ma=no&datalen=640；读取 2026-11-17T22:47:10.120314+08:00；SHA256 `41e097c5ca6e2255ca828d1731ae0e3b41cbb12c55c3fbc21f18dc1e2a47c816`。
- 000032: https://fund.eastmoney.com/pingzhongdata/000032.js；读取 2026-11-17T22:47:17.409224+08:00；SHA256 `afbb3d10afcac03eb1876b856c5973d6c36f286fc26b636360da58c8131e9c1b`。
- 002340: https://fund.eastmoney.com/pingzhongdata/002340.js；读取 2026-11-17T22:47:17.456594+08:00；SHA256 `daf70ee123b076c345e1688a3fcae1222666a2f7276cf1501cfcce709ab63834`。
- 022463: https://fund.eastmoney.com/pingzhongdata/022463.js；读取 2026-11-17T22:47:17.476210+08:00；SHA256 `5527ba85aadd36146e58de4b9c0596214063dd14920e1a26e88078e01da37a88`。
- 040046: https://fund.eastmoney.com/pingzhongdata/040046.js；读取 2026-11-17T22:47:17.792136+08:00；SHA256 `f0f184b40b3c3b39664c66729c04ac4a4d34811f6e603dfc51df1b2fea953dac`。

选定指数原始观测快照见 [live-index-evidence-2026-11-17.json](../examples/research/live-index-evidence-2026-11-17.json)，包含重新取数时使用的256个历史点和来源哈希；专项测试已从该快照复算上述统计。
