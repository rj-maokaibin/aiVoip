# VOIP Analyzer Golden Cases

Golden Case 分为两类：

1. **Field Golden Case**：来自真实现场并有人工作为 Ground Truth，例如 `APF1250_CS20260807_6886043`。
2. **Synthetic Golden Case**：算法输入由确定参数合成，Ground Truth 精确已知，用于边界/回归，例如 RTP 连续4包丢失、350ms静音、86ms回声。

原则：
- Field Case 用于验证“真实现场是否解释正确”。
- Synthetic Case 用于验证“算法数值/事件边界是否精确”。
- Analyzer/Rule/Prompt 升级必须至少跑全部 Synthetic Golden；有真实源文件时再跑 Field Golden。
- 历史 Golden 结果不得覆盖，必须记录 analyzer/rule/workflow 版本。
