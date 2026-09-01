.PHONY: check check-verbose serve help history validate

# 运行价格监控
check:
	python3 check_prices.py

# 运行价格监控（详细输出）
check-verbose:
	python3 check_prices.py --verbose

# 启动本地 HTTP 服务查看看板（默认 8080 端口）
serve:
	python3 -m http.server 8080

# 查看价格变动历史
history:
	python3 -m json.tool price_history.json | less

# 验证数据文件格式
validate:
	python3 -c "import json; json.load(open('data.json')); print('data.json OK')"
	python3 -c "import json; json.load(open('price_history.json')); print('price_history.json OK')"
	python3 -c "import ast; ast.parse(open('check_prices.py').read()); print('check_prices.py OK')"

help:
	@echo "可用命令:"
	@echo "  make check          - 运行价格监控"
	@echo "  make check-verbose  - 运行价格监控（详细输出）"
	@echo "  make serve          - 启动本地看板服务 (http://localhost:8080)"
	@echo "  make history        - 查看价格变动历史"
	@echo "  make validate       - 验证数据文件格式"
