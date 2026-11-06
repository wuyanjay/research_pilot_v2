# 标题/正文检索与分组稳健选参（中等，v3）
研究问题：标题加权能否改善导航类检索，同时避免牺牲主题类查询？
documents含id,title,text，查询含id,text,group；dev_queries额外有relevant，test_queries没有标注。
标题和正文分别独立建立BM25统计：lowercase regex [a-z0-9]+；df按文档计，N包括空字段文档，字段平均长度为0时按1处理。每词分数=ln(1+(N-df+.5)/(df+.5))*f*(k1+1)/(f+k1*(1-b+b*length/avg_length))。f=0贡献0；重复查询词重复贡献。
总分=正文BM25+title_weight*标题BM25，两字段共用k1,b。所有文档按(-score,id)排列，允许整体正比例缩放而不改变排名。
基线为default_config，候选遍历grid全部27种配置，返回每种的开发集完整排名、普通查询均值Recall@5/MRR@10以及group_metrics。
每组单独计算Recall@5百分制和MRR@10。group_metrics.groups保存每组结果；worst_mrr_at_10是最差组MRR；macro_mrr_at_10和macro_recall_at_5_percent是各组等权平均（不能按组查询数量加权）。
按worst_mrr_at_10最大，再macro_mrr_at_10最大，再macro_recall_at_5_percent最大选参；比较前把这三项四舍五入到小数点后12位，仍并列则按title_weight,k1,b升序。普通MRR/Recall只报告，不作为选参目标。仅开发标注参与选参。
Recall@5=前5唯一相关文档数/全部相关文档数；MRR@10=前10首个相关位置倒数，未命中为0。最终测试指标为各主条件所有测试查询均值的条件均值。
run阶段返回baseline,candidate和details.selected/search；rank阶段仅按data.selected返回candidate排名，不再选参。输出所有文档、所有查询。
数据为赛事自制合成数据，分组qrels由不同标题敏感程度的教师配置产生，并非论文或人工相关性标注。保留测试只覆盖已公开的字段长度差异、空标题、重复/未知词、组大小不等、并列与不同语料；不增加秘密规则。
