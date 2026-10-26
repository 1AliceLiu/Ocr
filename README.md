包含的容器：
osd-daily 用于每天北京时间6点30分刷新orc/out/streams.txt、devices.csv、channels.csv列表，需要配合重启osd20才可检测生效
osd20 用于检测的主程序
monitor
osd-error-prune 用于 删掉orc/output/osd_error.txt中前一天的数据
api 用于调用osd_error.txt中的结果