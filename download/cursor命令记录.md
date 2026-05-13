
# 使用python playwright打开https://web.telegram.org/a/#-1001395144198 ，dom： @pua1.html ;
这里可以滚动加载更多消息：（class="Transition MessageList custom-scroll no-avatars no-composer with-default-bg scrolled"），其中，子标签的子标签（class="messages-container"），里面是消息组（可以通过滚动加载更多）。
1，每个组里面@download/resources/pua1.html:5600-5601 包含时间和消息内容，消息内容里面可能是文件、文字、图片、图文、视频等；
也包含消息组的时间戳：@download/resources/pua1.html:5601-5603或@download/resources/吃瓜大佬2--图文集.html:5586-5587  ；


1,2    每条消息也可能包含@download/resources/吃瓜大佬2--图文集.html:5671-5672 文本，也要保存，命名格式：messageid_textpre_text;其中textpre是文本的前30个字符,如果没有text，则记为”无文本“；

1.3  所有文件保存到目录 @pua1 下面的对应的组里面(命名格式：group_时间戳_N，N是指第n组；


2，如果massage是文件@download/resources/pua1.html:5603-5604 ，鼠标右键，点击”Download“，就会自动下载到”D:\Download“目录下，文件名： @download/resources/pua1.html:5620-5625 ,等到下载完成后，把文件剪切到group目录，文件重命名格式：messageid_textpre_file_finename；


3，如果message是视频的话，@download/resources/pua1.html:6118-6119 ，里面会包含视频@download/resources/pua1.html:6130-6134 、图片@download/resources/pua1.html:6135-6140 ，都要保存，视频可以点击右键然后点击download,过一段时间，视频下载完后，下载目录里会出现一个文件（video_2021-10-23_18-05-50.mp4），文件名里面包含视频最初的上传时间，然后剪切到组目录里面，文件重命名格式messageid_textpre_video_上传时间戳；图片命名格式messageid_textpre_video_image;


4，message也可能是图片视频集， @download/resources/吃瓜大佬.html:6056-6256 ；这是集id也是messageid @download/resources/吃瓜大佬.html:6056-6057 ，集下面的每条图片或视频，有自己的id 也就是子id@download/resources/吃瓜大佬.html:6070-6071 @download/resources/吃瓜大佬.html:6092-6093 ，保存的图片或视频的命名格式：messageid_textpre_子id_video_上传时间戳、messageid_textpre_子id_video_image（视频封面）、messageid_子id_textpre_img（图片）；

5,  每条消息可能包含评论
@download/resources/反差婊.html:3571-3574 ，每条评论里面可能包含图片、视频、图片视频集、文本；
需要保存评论内容，保存目录：同上——组目录；
评论中的所有文本，保存文件命名格式：messageid_textpre_comments_评论数；
评论中的图片视频集保存的命名格式：messageid_textpre_comments_cmt-text-pre_img（评论中的图片），messageid_textpre_comments_cmt-text-pre_video_上传时间戳（评论中的视频），messageid_textpre_comments_cmt-text-pre_video_img（评论中的视频的封面）；
cmt-text-pre是指评论的文字内容的前30个字符(没有的话记为”无文字“)；
评论内容：@download/resources/反差婊-评论.html:4747-4748下面的平级标签message-group都是 @download/resources/反差婊-评论.html:4759-4760 @download/resources/反差婊-评论.html:4818-4819 评论内容组；
group下面的每个message是每一条评论；
评论者用户名：
@download/resources/反差婊-评论.html:4770-4776 ，评论文本内容 @download/resources/反差婊-评论.html:4776-4779 ；
也可能评论里面是图片视频集@download/resources/反差婊-评论.html:4838-4980 （参考聊天消息中的图片视频集的处理方式） 。

# 浏览器
浏览器相关代码参考：
@create_book_起点.py 

@download/telegram_web_download_pua1.py:37-38 使用这个啊，参考： @download/resources/create_book_起点.py:547-552 @download/resources/create_book_起点.py:618-625 

# 目录
@download/telegram_web_download_pua1.py:444-445 应该是当前消息组都下载完了，然后再滚动加载更多；

@pua1 应该是在@output 下面；
@group_January 17_0的时间戳应该解析为yyyymmdd格式 

# 进度-起点
跳转页面之前，增加一步：
先打开：https://web.telegram.org/a/#-5132543141 ，如果有@download/resources/go_to_bottom.html:1 ，就点一下；
然后点击最后一个group的最后一个message的右箭头，实现跳转， 跳转后的url地址：https://web.telegram.org/a/#-1001395144198 （已经配置在项目里了）；
然后进行业务流程

@download/telegram_web_download_pua1.py:198-199 报错。dom： @pua2--tmp.html 

@download/telegram_web_download_pua1.py:179-180 这个取错了，应该是@download/resources/download_flag.html:1766-1767 ,不是"button.message-action-button" @download/telegram_web_download_pua1.py:180-181 

@download/telegram_web_download_pua1.py:180-181 这里还是选错了，dom： @download/resources/download_flag.html:1765-1772 
不能使用更精准的定位方法吗

你再检查下，会不会定位到其他同类的标签上

@download/telegram_web_download_pua1.py:186-187 试试先把鼠标放上去，然后停顿1s，再点击

# groups
@download/telegram_web_download_pua1.py:524-525 这里又找错了，应该是在（@download/resources/pua1.html:5582-5583 ）下面，再去找 @download/resources/pua1.html:5599-5879 

还是不对啊，怎么不按照我给你指定的class呢 @download/resources/pua1.html:5582-5583 

@download/resources/pua1.html:5582-7039 很好，class找对了，可是class="message-date-group"你没找到.参考方案： @download/问题/groups查找方案.md 




# todo：
@download/telegram_web_download_pua1.py:188-189 我看了，鼠标放的地方，是跳转箭头，可是，click之后，虽然跳过去了，但是，消息列表的位置不准确，跟我手动点击的不一样，代码点击之后的消息位置在实际的位置后面的几条消息的位置
