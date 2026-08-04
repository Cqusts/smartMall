# 通用 Java 服务镜像。构建上下文为 apps/java/，通过 --build-arg MODULE=mall-xxx 指定模块。
#
#   docker build --build-arg MODULE=mall-product -t smartmall/mall-product apps/java

# ---------- 构建阶段 ----------
FROM maven:3.9-eclipse-temurin-21 AS build

WORKDIR /src

# 先只拷 pom，让依赖下载结果进入独立的镜像层——改代码时不重新下载依赖
COPY pom.xml ./
COPY mall-common/pom.xml    mall-common/
COPY mall-gateway/pom.xml   mall-gateway/
COPY mall-product/pom.xml   mall-product/
COPY mall-asset/pom.xml     mall-asset/
COPY mall-dataplat/pom.xml  mall-dataplat/
COPY mall-kpi/pom.xml       mall-kpi/
RUN mvn -B -q dependency:go-offline -DskipTests || true

COPY . .

ARG MODULE
RUN test -n "$MODULE" || (echo "必须指定 --build-arg MODULE=mall-xxx" && exit 1) \
    && mvn -B -q -pl "$MODULE" -am -DskipTests clean package \
    && cp "$MODULE"/target/*.jar /app.jar

# ---------- 运行阶段 ----------
FROM eclipse-temurin:21-jre-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# 非 root 运行
RUN useradd -r -u 1001 -s /usr/sbin/nologin smartmall
WORKDIR /app
COPY --from=build /app.jar ./app.jar
RUN chown -R smartmall:smartmall /app
USER smartmall

ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=70 -XX:+UseG1GC -Duser.timezone=Asia/Shanghai -Dfile.encoding=UTF-8"

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
