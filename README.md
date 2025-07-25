# CHD 2 OpenAI

长安大学有自己的[ai门户](https://agi.chd.edu.cn/)，但小沙同学没有提供公开的API供第三方应用使用，实际体验不佳。

> 从cookie中的dify_app_config里能扒拉出这个`http://10.129.3.5:91/v1` 但是连不上

类似dify2openai，我在想，能不能整一个api代理，将学校的奇怪网页转换成openai兼容格式，供翻译插件使用呢？

说到做到，我让deepseek写了一段

## 用法
设置环境变量
1. `CONFIG_URL`  
   登录[ai门户](https://agi.chd.edu.cn/)，选择应用后，标题的链接  
   形如`https://agi.chd.edu.cn/chat?userToken={uuid}&appId={uuid}`  
   程序使用这个链接自动配置认证信息，链接存在有效期，过期无法使用

2. `AUTO_DELETE_CONVERSATIONS`  
   `true/false`是否使用自动删除  
    可以留空，默认为true，对话完成后会自动删除，这样不会在网页端看到大量堆积

接下来，直接`python chd2openai.py`就可以了，记得先在虚拟环境中安装依赖requests和flask  
把终端里的链接`http://127.0.0.1:5000`丢给你的客户端，享受不要钱的满血deepseek吧！

## 注意事项

1. 身份认证经常到期，目前没有设置更新API，需要重新配置`CONFIG_URL`重启
2. 和OpenAI的API不同，学校的API是有状态的，每个对话有自己的ID，没法像OpenAI那样连着以前的会话一起发。目前简单粗暴地仅允许一次对话，对话完了立刻删除。
3. OpenAI的API中模型选择也是无效的，门户网页中不同的应用分别是不同的模型和prompt，对应着不同的`CONFIG_URL`

## 一些观察

1. `CONFIG_URL`很快过期，但认证完成后能正常使用数天
2. 模型选择或许并非无法实现，可以做成提交`CONFIG_URL`来添加/更新模型的方式，提供认证信息存储管理
   对于不同的模型，`CONFIG_URL`中userToken是相同的，appId是不变的，可以提供API提交appId添加更多模型，提交userToken刷新认证信息
3. 学校部署的DS非常慢，但真的是671B，fp8的完整模型
