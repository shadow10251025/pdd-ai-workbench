"""
拼多多一键上架 - 管理端（总控台）启动器

与用户端 launcher.py 唯一的区别：把 license.ADMIN_MODE 置为 True，
从而：
  1. 免激活码（出租方自用）；
  2. 前端显示「管理」页签，可发码 / 查码 / 记账 / 统计 / 导出。

管理端打包时会额外携带 keygen/private_key.pem + keygen/licenses.db（发码所需），
务必只留给自己，绝不能发给租户。
"""
import license
import launcher

# 必须先置位再调 main()：main() 里会做授权校验，ADMIN_MODE=True 直接放行。
license.ADMIN_MODE = True


if __name__ == '__main__':
    launcher.main()
