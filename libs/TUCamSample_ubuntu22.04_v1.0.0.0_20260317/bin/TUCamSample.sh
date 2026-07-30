#!/bin/bash

kaya_name="libKYFGLibGenTL.cti"
flex_name="cxplink_gentl.cti"

tmp_file="tucsen_tmp_file.txt"
ld_tmp_file="tucsen_ld_tmp_file.txt"

default_str=""
flex_path=$default_str
libPocoFoundationName="libPocoFoundation.so.tucsen"

#====================================================================================================================================================
#判断该目录是否有效，有效则赋值给对应的全局变量且返回1，无效则返回0
#输入参数$1：某采集卡cti文件的全路径，比如/opt/KAYA_Instruments/lib/libKYFGLibGenTL.cti

function is_dir_valid()
{
	dir_path=$(dirname $1)
  		
  	if [[ $1 == *$flex_name* ]]; then
		#使用find命令查找该文件夹下是否有so文件
		so_file_count=$(find $dir_path -type f -name "*.so*" | wc -l 2> /dev/null)
		top2_dir=$(dirname $(dirname $(dirname $1)))
		#使用find命令查找该上上级目录下是否有install_driver.sh脚本
		install_file_count=$(find $top2_dir -maxdepth 1 -type f -name "install_driver" | wc -l 2> /dev/null)
		#使用find命令查找该上上级目录下是否有uninstall_driver.sh脚本
		uninstall_sh_count=$(find $top2_dir -maxdepth 1 -type f -name "uninstall_driver" | wc -l 2> /dev/null)
		#使用find命令查找上上级目录下是否有versioninfo
		cur_ver_count=$(find $top2_dir -maxdepth 1 -type f -name "versioninfo" | wc -l 2> /dev/null)
		echo "so_count=$so_file_count, install_count=$install_file_count, uninstall_count=$uninstall_sh_count, cur_ver_count=$cur_ver_count"
		if [ $so_file_count -gt 0 -a $install_file_count -gt 0 -a $uninstall_sh_count -gt 0 -a $cur_ver_count -gt 0 ]; then
			#若当前目录下so文件个数大于0, 上上级目录下有uninstall_driver.sh且有install_driver.sh,且上上级目录下有versioninfo文件,则认为这是安装之后的目录，而不是安装包
  			return 1
  		fi
  	fi
	return 0
}

#====================================================================================================================================================
#获取samadhi采集卡驱动cti文件所在的目录
#输入参数$1：采集卡cti的文件名，cxplink_gentl.cti

function get_samadhi_interface_card_dir()
{
	max_ver_text="VERSION: 0.0.0"
	echo "------------------ start find $1 ------------------"
	#使用find命令查找文件
	(find / -type f -name "$1" > $tmp_file 2> /dev/null)

	while read -r var; do
		line=$var
    		echo "find dir:$var"
  		is_dir_valid $var
  		result=$?
  		if [ $result -eq 0 ]; then
  			echo "!!! but this dir is invalid"
			continue;
  		fi
		top2_dir=$(dirname $(dirname $(dirname $line)))
		#使用find命令找到上上级目录的versioninfo文件，然后读出其第一行内容
		ver_path=$(find $top2_dir -maxdepth 1 -type f -name "versioninfo" 2> /dev/null)
		text=$(head -n 1 $ver_path)
		echo "$text"
		#比较第一行内容（也就是版本号），找出最新版本
		if [ "$text" \> "$max_ver_text" ]; then
			max_ver_text=$text
			flex_path=$line
		fi
	done < "$tmp_file"

	echo "------------------ end find $1 ------------------"
	echo ""
	
}

#====================================================================================================================================================
#给kaya的libPocoFoundation.so.62建立软连接

function ldd_kaya_libPoco()
{
	#先删除libPocoFoundation.so.tucsen软链接
	rm -rf $libPocoFoundationName

	kaya_cti_path=$(echo "$KAYA_VISION_POINT_LIB_PATH"/$kaya_name)
	echo "kaya_cti_path=$kaya_cti_path"
	if [ ! -f "$kaya_cti_path" ];then
		return
	fi

	#使用ldd命令查找该cti依赖的库
	(ldd "$kaya_cti_path" > $ld_tmp_file 2> /dev/null)

	while read -r var; do
    		if [[ $var == *"=>"* ]]; then
   			#如果当前行有依赖库，比如libm.so.6 => /lib/x86_64-linux-gnu/libm.so.6 (0x0000768a5b2ad000)
  			#截取第一个"=> /"右边的字符串
			right=$(echo ${var#*"=> /"})
			if [[ $right != *"/"* ]]; then
				#若right中已经没有/符号，则continue
				continue
			fi

			#若是kaya的，则需检查是否需要为其建立软链接
			#截取最后一个" (0x"左边的字符串
			relate_path=/$(echo ${right%" (0x"*})
			if [[ $relate_path == *"libPocoFoundation.so"* ]]; then
				#如果当前依赖库路径中包含了“libPocoFoundation.so”这个关键字，则为其建立一个软链接
				(ln -sf $relate_path $libPocoFoundationName)
				return
			fi
  		else
  			continue
		fi
	done < "$ld_tmp_file"
}

#===============================================================main=================================================================================
#给kaya的libPocoFoundation.so.62建立软连接
#ldd_kaya_libPoco
#ls -la $libPocoFoundationName

##判断samadhi的环境变量是否为空
#oldFixlEnvPath=$(echo $GENICAM_GENTL64_PATH_TUCSEN)
#if [[ -z "$oldFixlEnvPath" ]]; then
#	#查找本机上最新版本的samadhi的cti全路经
#	get_samadhi_interface_card_dir $flex_name
#	#设置samadhi环境变量
#	export GENICAM_GENTL64_PATH_TUCSEN="$flex_path"
#fi
#echo "GENICAM_GENTL64_PATH_TUCSEN=$GENICAM_GENTL64_PATH_TUCSEN"

#删除临时文件
rm -rf $tmp_file
rm -rf $ld_tmp_file

release_num=$(lsb_release -r --short)
ubuntu_folder="2204"

if [[ $release_num == *"18."* ]]; then
	ubuntu_folder="1804"
elif [[ $release_num == *"20."* ]]; then
	ubuntu_folder="2004"
else
	ubuntu_folder="2204"
fi

export GENICAM_GENTL64_PATH_TUCSEN="/opt/samadhi/usr/lib"

appname=$(basename "$0" | sed 's/\.sh$//')
dirname=$(dirname "$0")
dirpath=$(cd "$dirname" && pwd)
export LD_LIBRARY_PATH="$dirpath:$dirpath/$ubuntu_folder"
echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
"$dirpath/$ubuntu_folder/$appname" "$@"


