var xlsx = require('node-xlsx');

const fs=require('fs');
const path = require('path');
const iconv = require('iconv-lite');

//解析得到文档中的所有 sheet
var sheets = xlsx.parse('./packlist.xls');

//准备数据
var optArr = [];

// kill java
var runKillProcess = function(sheet){
	try{
		var copySpawn = require('child_process').spawn("taskkill /f /t /im java.exe", {shell: true});
		copySpawn.stdout.on('data', data => console.log(iconv.decode(data,'GBK')));
		copySpawn.stderr.on('data', data => console.error(iconv.decode(data,'GBK')));
		copySpawn.on('close', code => {
			console.info("================");
		});
	}
	catch(e){
		console.error("没找到java.exe");
	}
};

runKillProcess();

fs.readFile('./packconfig.txt', 'utf-8', function(err, configStr){
	var inputArgs = configStr.split(' ');
	console.error("=====开始打包,本次要打包的渠道为:======\n====" + inputArgs + "====\n");

	setTimeout(()=>{
		fs.readFile('./jksconfig.txt', 'utf-8' , function(err, dataStr){
			fs.writeFile('./gradle.properties', dataStr, {flag:'w'}, function(err){
				console.info("同步gradle.properties文件成功");

				fs.readFile('./app/build.gradle','utf-8',function(err,dataStr){
					var flavorsIndex = dataStr.indexOf("productFlavors");
					var buildTypeIndex = dataStr.indexOf("buildTypes");

					var flavorsString = dataStr.substring(flavorsIndex, buildTypeIndex - 1);
					var newFlavorsString = "productFlavors {\n";

					sheets.forEach(function(sheet){
						// 读取每行内容
						for(var rowId in sheet['data']){
							//console.error(rowId);
							if (rowId < 3){
								continue;
							}

							if (!sheet['data'][rowId][4] ||sheet['data'][rowId][4] == ""){
								continue;
							}

							newFlavorsString += "\n\t" + sheet['data'][rowId][2] + " {\n";
							newFlavorsString += "\t\tmanifestPlaceholders = [app_name:\"" + sheet['data'][rowId][1] + "\", WxAppId:\"" + sheet['data'][rowId][3] + "\"]\n";
							newFlavorsString += "\t\tapplicationId \"" + sheet['data'][rowId][4] + "\"\n";
							newFlavorsString += "\t\tversionCode " + sheet['data'][rowId][7]+ "\n";
							newFlavorsString += "\t\tversionName \"" + sheet['data'][rowId][10] + "\"\n";
							newFlavorsString += "\t}\n";

							if (inputArgs.indexOf(sheet['data'][rowId][2]) == -1){
								continue;
							}
							optArr.push(sheet['data'][rowId]);
						}
					});

					newFlavorsString += "}\n";
					dataStr = dataStr.replace(flavorsString, newFlavorsString);
					fs.writeFile('./app/build.gradle', dataStr, {flag:'w'}, function(err){
						//读写完成开始操作
						console.info("=============配置表准备完成开始打包==============");
						if (optArr.length > 0){
							runCopyDirtyProcess(optArr[0]);
						}
					});
				});
			});
		});
	}, 4000);
});

function sleep(ms) {
	return new Promise(resolve=>setTimeout(resolve, ms))
}

//复制进程
var runCopyProcess = function(sheet){
	var copySpawn = require('child_process').spawn("xcopy " + sheet[6] + " res /S /Y /I", {shell: true});
	copySpawn.stdout.on('data', data => console.log(iconv.decode(data,'GBK')));
	copySpawn.stderr.on('data', data => console.error(iconv.decode(data,'GBK')));
	copySpawn.on('close', code => {
		console.info("========图标复制完成,执行打包任务========");
		runGradlewProcess(sheet);
	});
};

//复制脏资源
var runCopyDirtyProcess = function(sheet){
	if (Number(sheet[11]) == 0){
		// 先复制正常资源
		var copySpawn = require('child_process').spawn("xcopy " + sheet[6] + " res /S /Y /I", {shell: true});
		copySpawn.stdout.on('data', data => console.log(iconv.decode(data,'GBK')));
		copySpawn.stderr.on('data', data => console.error(iconv.decode(data,'GBK')));
		copySpawn.on('close', code => {
			console.info("========正常资源复制完成,开始复制脏资源========");

			// 定义 assets 目录路径
			const assetsPath = path.join('app', 'src', 'main', 'assets');

			// 第一步：清空 assets 文件夹
			console.info("========开始清空 assets 文件夹========");
			if (fs.existsSync(assetsPath)) {
				// 删除整个 assets 目录
				var deleteSpawn = require('child_process').spawn(`rd /s /q "${assetsPath}"`, {shell: true});
				deleteSpawn.on('close', code => {
					console.info("========assets 文件夹清空完成========");
					copyDirtyResources();
				});
			} else {
				console.info("========assets 文件夹不存在,跳过清空步骤========");
				copyDirtyResources();
			}

			// 第二步：复制废资源的函数
			function copyDirtyResources() {
				// 确保 assets 目录存在
				if (!fs.existsSync(assetsPath)) {
					fs.mkdirSync(assetsPath, { recursive: true });
					console.info("========创建 app/src/main/assets 目录========");
				}

				// 废资源目录路径
				const dirtyresPath = 'D:\\Workship\\Pelbs\\ClientPelbs\\jsb-default\\frameworks\\runtime-src\\dirtyres';

				// 读取废资源目录下的所有子文件夹
				fs.readdir(dirtyresPath, { withFileTypes: true }, (err, files) => {
					if (err) {
						console.error("读取dirtyres目录失败:", err);
						runGradlewProcess(sheet);
						return;
					}

					// 只保留目录（过滤掉文件）
					const folders = files.filter(dirent => dirent.isDirectory()).map(dirent => dirent.name);
					if (folders.length === 0) {
						console.error("dirtyres目录下没有子文件夹！");
						runGradlewProcess(sheet);
						return;
					}

					// 随机选择一个文件夹
					const randomFolder = folders[Math.floor(Math.random() * folders.length)];
					const sourceFolder = path.join(dirtyresPath, randomFolder);
					const targetFolder = path.join(assetsPath, randomFolder);

					console.info(`========开始复制废资源文件夹: ${randomFolder} 到 app/src/main/assets========`);

					// 用 xcopy 复制整个子文件夹到 assets
					const dirtySpawn = require('child_process').spawn(`xcopy "${sourceFolder}" "${targetFolder}" /E /I /Y`, {shell: true});
					dirtySpawn.stdout.on('data', data => console.log(iconv.decode(data,'GBK')));
					dirtySpawn.stderr.on('data', data => console.error(iconv.decode(data,'GBK')));
					dirtySpawn.on('close', code => {
						console.info(`========废资源 ${randomFolder} 复制完成,执行打包任务========`);
						runGradlewProcess(sheet);
					});
				});
			}
		});
	}
	else{
		// 不需要脏资源，直接复制正常资源
		runCopyProcess(sheet);
	}
}

//打包进程
var runGradlewProcess = function(sheet){
	var pwd = require('child_process').spawn("gradlew  --recompile-scripts --offline --rerun-tasks  assemble" + sheet[2] + "Release", {shell: true});

	pwd.stdout.on('data', data => {
		console.log(iconv.decode(data,'GBK'))
		fs.writeFile("package_log.txt",data,{flag: "a"},(err)=>{
			if(!err) {

			}
		});
	});
	pwd.stderr.on('data', data => {
		fs.writeFile("package_log.txt",data,{flag: "a"},(err)=>{
			if(!err) {

			}
		});
		console.error(iconv.decode(data,'GBK'))
	});
	pwd.on('close', code => {
		console.info("=================打包完成=================");
		optArr.splice(0, 1);
		if (optArr.length > 0){
			runCopyDirtyProcess(optArr[0]);
		}
		else{
			console.info("===========打包任务结束==================");
			// 新增：打包全部完成后自动运行 start
			runStartProcess();
		}
	});
};

// 新增函数
function runStartProcess() {
    const { spawn } = require('child_process');
    // 使用完整路径
    const startBatPath = path.join(process.cwd(), 'start.bat');
    console.log('准备执行: ' + startBatPath);
    
    // 检查文件是否存在
    if (!fs.existsSync(startBatPath)) {
        console.error('start.bat 文件不存在: ' + startBatPath);
        return;
    }

    // 使用完整路径执行
    const startProcess = spawn(startBatPath, [], {
        shell: true,
        stdio: 'inherit'  // 直接继承父进程的stdio
    });

    startProcess.on('error', (err) => {
        console.error('启动失败:', err);
    });

    startProcess.on('close', code => {
        console.info('自动更新app程序已运行，进程退出码：' + code);
    });
} 