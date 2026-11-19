# 本地 PostgreSQL 与 pgvector

生产目标栈采用PostgreSQL/pgvector。迁移`0007_pgvector`只在PostgreSQL创建1024维向量表和cosine HNSW索引；SQLite保留普通参考路径。向量行绑定文档版本、解析批次、切分批次、块及provider/model修订。相似度只排序候选，返回前仍逐版本核查当前发布资格和`document.read`/`knowledge.query`授权。

Python依赖`pgvector`提供SQLAlchemy `Vector(1024)`绑定。安装该Python包不会安装数据库端扩展；PostgreSQL必须具备pgvector extension后才能执行0007。Embedding provider仍由逐服务批准控制，当前代码不自动调用BGE，不把占位向量当内容嵌入或质量结果。

临时集成测试通过`DATACENTER_TEST_POSTGRES_URL`显式连接专用127.0.0.1数据库，并检查数据库名前缀和`vector`扩展。命令/容器名/数据卷及实际结果由本地栈证据登记。不得指向默认`outputs/data-center.db`或企业库；迁移前先检查URL与数据库身份。

目标集群不得通过`Base.metadata.create_all`代替Alembic。`init_db`必须使用显式匹配的Settings/engine，经[安全迁移](安全迁移与事故核查.md)绑定目标、前态与受控事务连接；裸Alembic与启动自动升级均关闭，非空旧PG升级需停写备份及另行评审。向量表使用独立SQLAlchemy metadata，0001和SQLite普通`create_all`不会提前创建它；0007先装扩展再建1024维列和HNSW cosine索引。0007不可降级删除向量和来源关联；索引可重建但须遵守保留和当前权限规则。

2026-11-17本地证据：官方`pgvector/pgvector:pg18`容器、PostgreSQL18.7/pgvector0.8.7，独立数据库名`dc_local_pgvector_synthetic`、仅映射`127.0.0.1:55440`。0001–0007真实迁移通过；1024维往返/cosine、合成发布版本写入与授权检索、撤权拒绝、来源/模型绑定与非法向量拒绝9项通过。精确命令、源码hash和限制见`outputs/local-stack/2026-11-17/postgres/pgvector-evidence.json`。固定向量只证明存取与权限路径，不证明BGE语义质量。

当前应用的关键词检索仍是默认，`authorized_nearest`只作为显式API候选实现、尚未替换生产查询。只有获准的向量provider、真实质量样本和混合检索评测具备后，才可切换正式查询；pgvector存取检查不能证明召回质量或首期闭环。
