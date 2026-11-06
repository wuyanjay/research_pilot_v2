FROM agenthack/base:1.1.1

WORKDIR /app

COPY --chown=agent:agent requirements.txt ./requirements.txt
COPY --chown=agent:agent vendor/ ./vendor/
# PDF wheel 与预训练语言文件提前准备；构建和评测期间均无需联网下载。
RUN cd /app && sha256sum -c vendor/SHA256SUMS \
    && python -m pip install --no-cache-dir --no-index --find-links=/app/vendor/wheels -r requirements.txt
ENV TESSDATA_PREFIX=/app/vendor/tessdata
# 容器始终使用赛事目录；宿主机 test_data/test_output 的绑定由 compose.test.yaml 指定。
ENV DATA_PATH=/data RESEARCH_RESULT_ROOT=/output
VOLUME ["/data", "/output"]

COPY --chown=agent:agent main.py ./main.py
COPY --chown=agent:agent src/ ./src/

USER agent
EXPOSE 8080
CMD ["python", "main.py"]
