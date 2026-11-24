一、构建镜像
cd ~/cca-sky9/CCA5
docker build -t quic_sky:latest .

cd ~/cca-sky9

二、创建容器
docker run --privileged -itd \
  -v "$(pwd)":/workspace \
  --name quic_sky9 \
  quic_sky:latest \
  /bin/bash

三、进入容器
docker exec -it quic_sky9 /bin/bash

四、安装 SkyDiscover
cd /workspace/skydiscover
uv sync

看 optimize 的用法
uv run skydiscover optimize --help

uv run skydiscover optimize benchmarks/CCA5/initial_program.py \
  benchmarks/CCA5/evaluator/evaluator.py \
  -c benchmarks/CCA5/config.yaml 

  uv run skydiscover optimize initial_program.py evaluator.py \
	--config config.yaml \
	--search adaevolve \
	--model gpt-5 \
	--iterations 100 \
	--output outputs/my_problem