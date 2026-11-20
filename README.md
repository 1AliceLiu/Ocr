包含的容器：
osd-daily 用于每天北京时间6点30分刷新orc/out/streams.txt、devices.csv、channels.csv列表，需要配合重启osd20才可检测生效；同一容器每天6点刷新orc/out-hk03/下的三张表
osd20 用于检测的主程序
config/blocked_channels.txt 用于屏蔽旧平台通道编码前缀，一行一个，屏蔽后不抓帧也不查HK03
osd-error-prune 用于 删掉orc/output/osd_error.txt中前一天的数据
api 用于调用osd_error.txt中的结果