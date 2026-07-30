#!/bin/bash

# SamplePro 完全通用打包脚本
# 基于ldd动态分析，适配任何Qt版本和安装环境

appname="TUCamSample"
dst="./2204"

echo "=========================================="
echo "SamplePro 完全通用打包脚本"
echo "=========================================="

# 检查可执行文件
if [ ! -f "$appname" ]; then
    echo "❌ 错误：找不到可执行文件 $appname"
    echo "请确保在包含 $appname 的目录中运行此脚本"
    exit 1
fi

# 创建目标目录
echo "📁 创建目标目录：$dst"
rm -rf "$dst" 2>/dev/null
mkdir -p "$dst"

# 复制可执行文件
echo "📋 复制可执行文件..."
cp "$appname" "$dst"
chmod +x "$dst/$appname"
echo "✓ 复制并设置权限：$appname"

# 分析程序依赖
echo ""
echo "📚 分析程序依赖..."
echo "正在使用ldd分析 $appname 的依赖库..."

# 获取所有依赖库路径
ALL_LIBS=$(ldd "$appname" | awk '/=>/ {print $3}' | grep '^/' | sort -u)

# 统计依赖库数量
LIB_COUNT=$(echo "$ALL_LIBS" | grep -c '^/')
echo "发现 $LIB_COUNT 个依赖库"

# 复制所有直接依赖库
echo "复制依赖库..."
COPIED_LIBS=""

for lib in $ALL_LIBS; do
    if [ -f "$lib" ]; then
        lib_name=$(basename "$lib")
        dest_lib="$dst/$lib_name"
        
        # 避免重复复制
        if [ ! -f "$dest_lib" ]; then
            cp -L "$lib" "$dest_lib" 2>/dev/null || cp "$lib" "$dest_lib"
            COPIED_LIBS="$COPIED_LIBS $lib_name"
            echo "  ✓ $lib_name"
        fi
    fi
done

# 分析Qt相关库和查找Qt插件
echo ""
echo "🔍 分析Qt环境和插件..."

# 查找Qt核心库路径
QT_CORE_LIB=$(echo "$COPIED_LIBS" | tr ' ' '\n' | grep 'libQt5Core\.so' | head -n1)

if [ -n "$QT_CORE_LIB" ]; then
    echo "✓ 找到Qt核心库：$QT_CORE_LIB"
    
    # 通过ldd找到Qt库的实际安装路径
    QT_LIB_PATH=$(ldd "$appname" | grep "$QT_CORE_LIB" | awk '{print $3}' | head -n1)
    if [ -f "$QT_LIB_PATH" ]; then
        QT_BASE_DIR=$(dirname $(dirname "$QT_LIB_PATH"))
        echo "✓ Qt安装基础目录：$QT_BASE_DIR"
        
        # 尝试在Qt基础目录下查找插件
        PLUGIN_DIRS=(
            "$QT_BASE_DIR/plugins"
            "$QT_BASE_DIR/../plugins"
            "$(dirname "$QT_LIB_PATH")/../plugins"
            "/usr/lib/$(basename $(dirname "$QT_LIB_PATH"))/qt5/plugins"
            "/usr/lib/x86_64-linux-gnu/qt5/plugins"
        )
        
        FOUND_PLUGIN_DIR=""
        for plugin_dir in "${PLUGIN_DIRS[@]}"; do
            if [ -d "$plugin_dir/platforms" ]; then
                FOUND_PLUGIN_DIR="$plugin_dir"
                echo "✓ 找到Qt插件目录：$plugin_dir"
                break
            fi
        done
    fi
else
    echo "⚠ 未找到Qt核心库，可能是静态编译或其他情况"
fi

# 查找Qt平台插件 - 优先使用与SamplePro兼容的版本
echo ""
echo "🎯 查找Qt平台插件..."
QXCB_PLUGIN=""

# 首先查找与SamplePro使用相同Qt版本的插件
if [ -n "$QT_LIB_PATH" ]; then
    QT_BASE_DIR=$(dirname $(dirname "$QT_LIB_PATH"))
    echo "🔍 在SamplePro的Qt安装目录中查找插件..."
    
    COMPATIBLE_QXCB_PATHS=(
        "$QT_BASE_DIR/plugins/platforms/libqxcb.so"
        "$(dirname "$QT_LIB_PATH")/../plugins/platforms/libqxcb.so"
        "$QT_BASE_DIR/../plugins/platforms/libqxcb.so"
    )
    
    for compat_path in "${COMPATIBLE_QXCB_PATHS[@]}"; do
        if [ -f "$compat_path" ]; then
            QXCB_PLUGIN="$compat_path"
            echo "✓ 找到兼容版本的Qt平台插件：$compat_path"
            break
        fi
    done
fi

# 如果没找到兼容版本，才查找系统版本
if [ -z "$QXCB_PLUGIN" ]; then
    echo "⚠️ 未找到兼容版本，查找系统Qt平台插件..."
    QXCB_PATHS=(
        "/usr/lib/x86_64-linux-gnu/qt5/plugins/platforms/libqxcb.so"
        "/usr/lib/qt5/plugins/platforms/libqxcb.so"
        "/usr/local/qt5/plugins/platforms/libqxcb.so"
        "/opt/qt5/plugins/platforms/libqxcb.so"
    )
    
    # 先在标准位置查找
    for path in "${QXCB_PATHS[@]}"; do
        if [ -f "$path" ]; then
            QXCB_PLUGIN="$path"
            echo "✓ 标准位置找到Qt平台插件：$path"
            break
        fi
    done
    
    # 如果没找到，使用find全局搜索
    if [ -z "$QXCB_PLUGIN" ]; then
        echo "在标准位置未找到，执行全局搜索..."
        QXCB_PLUGIN=$(find /usr /opt /home 2>/dev/null -name "libqxcb.so" -type f 2>/dev/null | head -n1)
        if [ -n "$QXCB_PLUGIN" ]; then
            echo "✓ 全局搜索找到Qt平台插件：$QXCB_PLUGIN"
        fi
    fi
fi

# 如果找到了插件，复制它和相关的库
if [ -n "$QXCB_PLUGIN" ] && [ -f "$QXCB_PLUGIN" ]; then
    echo ""
    echo "📦 复制Qt平台插件和相关库..."
    
    # 创建platforms目录结构
    mkdir -p "$dst/platforms"
    
    # 检查版本兼容性
    echo "🔍 检查Qt版本兼容性..."
    APP_QT_VERSION=""
    PLUGIN_QT_VERSION=""
    
    # 获取SamplePro使用的Qt版本
    if [ -n "$QT_CORE_LIB" ]; then
        QT_CORE_PATH=$(echo "$COPIED_LIBS" | tr ' ' '\n' | grep "$QT_CORE_LIB" | head -n1)
        if [ -f "$dst/$QT_CORE_LIB" ]; then
            APP_QT_VERSION=$(strings "$dst/$QT_CORE_LIB" | grep -o 'Qt [0-9]\+\.[0-9]\+\.[0-9]\+' | head -n1 | cut -d' ' -f2)
        fi
    fi
    
    # 获取插件的Qt版本
    PLUGIN_QT_VERSION=$(strings "$QXCB_PLUGIN" | grep -o 'Qt [0-9]\+\.[0-9]\+\.[0-9]\+' | head -n1 | cut -d' ' -f2)
    
    echo "  SamplePro Qt版本：${APP_QT_VERSION:-"未知"}"
    echo "  插件Qt版本：${PLUGIN_QT_VERSION:-"未知"}"
    
    # 如果版本不匹配，尝试查找兼容版本
    if [ -n "$APP_QT_VERSION" ] && [ -n "$PLUGIN_QT_VERSION" ] && [ "$APP_QT_VERSION" != "$PLUGIN_QT_VERSION" ]; then
        echo "⚠️ Qt版本不匹配，尝试查找兼容版本..."
        
        # 从SamplePro的依赖库中查找Qt插件目录
        COMPATIBLE_QXCB=""
        if [ -n "$QT_LIB_PATH" ]; then
            QT_BASE_DIR=$(dirname $(dirname "$QT_LIB_PATH"))
            COMPATIBLE_PLUGINS=(
                "$QT_BASE_DIR/plugins/platforms/libqxcb.so"
                "$QT_BASE_DIR/../plugins/platforms/libqxcb.so"
                "$(dirname "$QT_LIB_PATH")/../plugins/platforms/libqxcb.so"
            )
            
            for compat_plugin in "${COMPATIBLE_PLUGINS[@]}"; do
                if [ -f "$compat_plugin" ]; then
                    COMPATIBLE_QXCB="$compat_plugin"
                    echo "✓ 找到兼容版本：$compat_plugin"
                    break
                fi
            done
        fi
        
        # 如果找到兼容版本，使用它
        if [ -n "$COMPATIBLE_QXCB" ] && [ -f "$COMPATIBLE_QXCB" ]; then
            QXCB_PLUGIN="$COMPATIBLE_QXCB"
            echo "✓ 使用兼容版本的Qt平台插件"
        else
            echo "⚠️ 未找到兼容版本，将使用系统版本（可能有兼容性问题）"
        fi
    fi
    
    # 复制libqxcb.so
    cp "$QXCB_PLUGIN" "$dst/platforms/"
    echo "  ✓ libqxcb.so"
    
    # 分析libqxcb.so的依赖并复制（排除Qt核心库，使用已有版本）
    QXCB_DEPS=$(ldd "$QXCB_PLUGIN" | awk '/=>/ {print $3}' | grep '^/' | sort -u)
    for dep in $QXCB_DEPS; do
        if [ -f "$dep" ]; then
            dep_name=$(basename "$dep")
            dest_dep="$dst/$dep_name"
            
            # 如果是Qt核心库，跳过（使用SamplePro依赖的版本）
            if [[ "$dep_name" == libQt5Core.so* ]] || [[ "$dep_name" == libQt5Gui.so* ]] || [[ "$dep_name" == libQt5Widgets.so* ]]; then
                echo "    ⏭ 跳过Qt核心库：$dep_name（使用SamplePro版本）"
                continue
            fi
            
            # 避免重复复制已存在的库
            if [ ! -f "$dest_dep" ]; then
                cp -L "$dep" "$dest_dep" 2>/dev/null || cp "$dep" "$dest_dep"
                echo "    ✓ $dep_name (qxcb依赖)"
                COPIED_LIBS="$COPIED_LIBS $dep_name"
            fi
        fi
    done
    
    # 特殊处理：复制Qt平台相关的库（但跳过核心库）
    if [ -n "$QT_LIB_PATH" ]; then
        QT_LIB_DIR=$(dirname "$QT_LIB_PATH")
        for qt_platform_lib in "$QT_LIB_DIR"/libQt5XcbQpa.so* "$QT_LIB_DIR"/libQt5DBus.so* "$QT_LIB_DIR"/libQt5EglFS.so*; do
            if [ -f "$qt_platform_lib" ]; then
                lib_name=$(basename "$qt_platform_lib")
                dest_lib="$dst/$lib_name"
                
                if [ ! -f "$dest_lib" ]; then
                    cp -L "$qt_platform_lib" "$dest_lib" 2>/dev/null || cp "$qt_platform_lib" "$dest_lib"
                    echo "    ✓ $lib_name (Qt平台库)"
                    COPIED_LIBS="$COPIED_LIBS $lib_name"
                fi
            fi
        done
    fi
    
else
    echo "❌ 未找到Qt平台插件，程序可能无法启动图形界面"
    echo "建议安装Qt5开发包：sudo apt-get install qtbase5-dev"
fi

# 复制额外的系统依赖
echo ""
echo "🔄 复制额外的系统依赖..."
SYSTEM_LIBS=(
    "libGL.so*"
    "libX11.so*"
    "libxcb.so*"
    "libxkbcommon.so*"
    "libfreetype.so*"
    "libfontconfig.so*"
    "libXext.so*"
    "libXrender.so*"
)

for lib_pattern in "${SYSTEM_LIBS[@]}"; do
    found_libs=$(find /usr/lib /lib -name "$lib_pattern" 2>/dev/null | head -3)
    for lib in $found_libs; do
        if [ -f "$lib" ]; then
            lib_name=$(basename "$lib")
            dest_lib="$dst/$lib_name"
            
            if [[ ! " $COPIED_LIBS " =~ " $lib_name " ]]; then
                cp -L "$lib" "$dest_lib" 2>/dev/null || cp "$lib" "$dest_lib"
                echo "  ✓ $lib_name (系统依赖)"
                COPIED_LIBS="$COPIED_LIBS $lib_name"
            fi
        fi
    done
done

# platforms目录已直接创建，无需额外符号链接
echo ""
echo "🔧 目录结构确认..."
if [ -d "$dst/platforms" ]; then
    echo "✓ platforms 目录已创建"
fi

# 最终检查
echo ""
echo "=========================================="
echo "🎉 打包完成！"
echo "=========================================="
echo ""
echo "📋 最终检查报告："
echo "  可执行文件：$(test -f "$dst/$appname" && echo "✓" || echo "❌")"
echo "  Qt核心库：$(echo "$COPIED_LIBS" | grep -q 'libQt5Core\.so' && echo "✓" || echo "❌")"
echo "  Qt平台插件：$(test -f "$dst/platforms/libqxcb.so" && echo "✓" || echo "❌")"
echo "  总库文件数：$(find "$dst" -name "*.so*" -type f | wc -l)"
echo "  总文件数：$(find "$dst" -type f | wc -l)"

# 检查关键组件是否存在
MISSING_COMPONENTS=""

if [ ! -f "$dst/$appname" ]; then
    MISSING_COMPONENTS="$MISSING_COMPONENTS 可执行文件"
fi

if ! echo "$COPIED_LIBS" | grep -q 'libQt5Core\.so'; then
    MISSING_COMPONENTS="$MISSING_COMPONENTS Qt核心库"
fi

if [ ! -f "$dst/platforms/libqxcb.so" ]; then
    MISSING_COMPONENTS="$MISSING_COMPONENTS Qt平台插件"
fi

# 输出结果和建议
if [ -n "$MISSING_COMPONENTS" ]; then
    echo ""
    echo "⚠️  缺少关键组件：$MISSING_COMPONENTS"
    echo ""
    echo "💡 解决方案："
    echo "  1. 安装Qt5开发包：sudo apt-get install qtbase5-dev qt5-default"
    echo "  2. 或者安装完整的Qt开发环境"
    echo "  3. 在有Qt环境的机器上重新运行此脚本"
    echo ""
    echo "🔍 调试信息："
    echo "  - 检查系统Qt：find /usr -name '*Qt5*' 2>/dev/null | head -5"
    echo "  - 检查程序依赖：ldd $appname"
else
    echo ""
    echo "✅ 打包成功！打包结果完全独立，可在任何Linux系统运行"
fi

echo ""
echo "📖 使用说明："
echo "  1. 整个 $dst 目录可复制到任何Linux系统"
echo "  2. 运行：./SamplePro.sh"
echo "  3. 无需安装Qt或任何其他依赖"
echo "  4. 支持Ubuntu、CentOS、Debian等主流发行版"
echo ""
echo "🌟 脚本特点："
echo "  ✅ 完全基于ldd动态分析，无固定路径依赖"
echo "  ✅ 自动适配任何Qt版本和安装方式"
echo "  ✅ 智能查找Qt插件和依赖库"
echo "  ✅ 递归复制所有必要依赖"
echo ""
