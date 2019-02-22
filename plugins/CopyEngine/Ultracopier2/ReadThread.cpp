#include "ReadThread.h"

#ifdef Q_OS_LINUX
#include <fcntl.h>
#endif
#include <sys/types.h>
#include <unistd.h>
#include "TransferThread.h"
#include "WriteThread.h"

ReadThread::ReadThread()
{
    stopIt=false;
    blockSize=ULTRACOPIER_PLUGIN_DEFAULT_BLOCK_SIZE*1024;
    setObjectName(QStringLiteral("read"));
    #ifdef ULTRACOPIER_PLUGIN_DEBUG
    stat=Idle;
    #endif
    isInReadLoop=false;
    tryStartRead=false;
    lastGoodPosition=0;
    file=NULL;
}

ReadThread::~ReadThread()
{
    stopIt=true;
    //disconnect(this);//-> do into ~TransferThread()
    //if(isOpen.available()<=0)
        emit internalStartClose();
}

void ReadThread::run()
{
    connect(this,&ReadThread::internalStartOpen,		this,&ReadThread::internalOpenSlot,     Qt::QueuedConnection);
    connect(this,&ReadThread::internalStartReopen,		this,&ReadThread::internalReopen,       Qt::QueuedConnection);
    connect(this,&ReadThread::internalStartRead,		this,&ReadThread::internalRead,         Qt::QueuedConnection);
    connect(this,&ReadThread::internalStartClose,		this,&ReadThread::internalCloseSlot,	Qt::QueuedConnection);
    connect(this,&ReadThread::checkIfIsWait,            this,&ReadThread::isInWait,             Qt::QueuedConnection);
}

void ReadThread::open(const std::string &file, const Ultracopier::CopyMode &mode)
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] open source: "+file);
    if(this->file!=NULL)
    {
        if(file==this->fileName)
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] Try reopen already opened same file: "+this->fileName);
        else
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Critical,"["+std::to_string(id)+"] previous file is already open: "+this->fileName);
        emit internalStartClose();
    }
    if(isInReadLoop)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Critical,"["+std::to_string(id)+"] previous file is already readding: "+this->fileName);
        return;
    }
    if(tryStartRead)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Critical,"["+std::to_string(id)+"] previous file is already try read: "+this->fileName);
        return;
    }
    stopIt=false;
    fakeMode=false;
    lastGoodPosition=0;
    this->fileName=file;
    this->mode=mode;
    emit internalStartOpen();
}

std::string ReadThread::errorString() const
{
    return errorString_internal;
}

void ReadThread::stop()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] stop()");
    stopIt=true;
    emit internalStartClose();
}

bool ReadThread::seek(const int64_t &position)
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start with: "+std::to_string(position));
    const off_t fsize = fseek(file, 0, SEEK_END);
    if(position>fsize)
        return false;
    return (fseek(file, position, SEEK_SET)==position);
}

int64_t ReadThread::size() const
{
    if(file==NULL)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] file not open to size it");
        return -1;
    }
    return fseek(file, 0, SEEK_END);
}

void ReadThread::postOperation()
{
    emit internalStartClose();
}

bool ReadThread::internalOpenSlot()
{
    return internalOpen();
}

bool ReadThread::internalOpen(bool resetLastGoodPosition)
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] internalOpen source: "+fileName+", open in write because move: "+std::to_string(mode==Ultracopier::Move));
    if(stopIt)
    {
        emit closed();
        return false;
    }
    #ifdef ULTRACOPIER_PLUGIN_DEBUG
    stat=InodeOperation;
    #endif
    if(file!=NULL)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] this file is already open: "+fileName);
        #ifdef ULTRACOPIER_PLUGIN_DEBUG
        stat=Idle;
        #endif
        emit closed();
        return false;
    }
    seekToZero=false;
    file=fopen(fileName.c_str(),"r");
    if(file!=NULL)
    {
        if(stopIt)
        {
            fclose(file);
            emit closed();
            return false;
        }
        #ifdef Q_OS_LINUX
        const int intfd=fileno(file);
        if(intfd!=-1)
        {
            posix_fadvise(intfd, 0, 0, POSIX_FADV_WILLNEED);
            posix_fadvise(intfd, 0, 0, POSIX_FADV_SEQUENTIAL);
            /*int flags = fcntl(intfd, F_GETFL, 0);
            fcntl(intfd, F_SETFL, flags | O_NONBLOCK);*/
        }
        #endif
        if(stopIt)
        {
            fclose(file);
            emit closed();
            return false;
        }
        size_at_open=fseek(file, 0, SEEK_END);
        mtime_at_open=TransferThread::readFileMDateTime(fileName);
        if(mtime_at_open<0)
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] Unable to read source mtime: "+fileName+", error: "+std::to_string(errno));
        if(resetLastGoodPosition)
            lastGoodPosition=0;
        if(!seek(lastGoodPosition))
        {
            fclose(file);
            errorString_internal="errno: "+std::to_string(errno);
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] Unable to seek after open: "+fileName+", error: "+std::to_string(errno));
            emit error();
            #ifdef ULTRACOPIER_PLUGIN_DEBUG
            stat=Idle;
            #endif
            return false;
        }
        emit opened();
        #ifdef ULTRACOPIER_PLUGIN_DEBUG
        stat=Idle;
        #endif
        return true;
    }
    else
    {
        errorString_internal="errno: "+std::to_string(errno);
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] Unable to open: "+fileName+", error: "+std::to_string(errno));
        emit error();
        #ifdef ULTRACOPIER_PLUGIN_DEBUG
        stat=Idle;
        #endif
        return false;
    }
}

void ReadThread::internalRead()
{
    isInReadLoop=true;
    tryStartRead=false;
    if(stopIt)
    {
        if(seekToZero && file!=NULL)
        {
            stopIt=false;
            lastGoodPosition=0;
            if(fseek(file,0,SEEK_SET)!=0)
            {
                ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] seek to 0 failed");
                isInReadLoop=false;
                internalClose();
                return;
            }
        }
        else
        {
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] stopIt == true, then quit");
            isInReadLoop=false;
            internalClose();
            return;
        }
    }
    #ifdef ULTRACOPIER_PLUGIN_DEBUG
    stat=InodeOperation;
    #endif
    int readSize=0;
    if(file==NULL)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] is not open!");
        isInReadLoop=false;
        return;
    }
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start the copy");
    emit readIsStarted();
    #ifdef ULTRACOPIER_PLUGIN_DEBUG
    stat=Idle;
    #endif
    if(stopIt)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] stopIt == true, then quit");
        isInReadLoop=false;
        internalClose();
        return;
    }
    do
    {
        //read one block
        #ifdef ULTRACOPIER_PLUGIN_DEBUG
        stat=Read;
        #endif
        readSize=-1;
        const size_t blockSize=sizeof(writeThread->blockArray);
        // if writeThread->blockArrayStart == writeThread->blockArrayStop then is empty
        if(writeThread->blockArrayStart<=writeThread->blockArrayStop)
        {
            if(writeThread->blockArrayStop==blockSize)
            {
                readSize=fread(writeThread->blockArray+writeThread->blockArrayStop,blockSize-writeThread->blockArrayStop,1,file);
                if(readSize>=0)
                    writeThread->blockArrayStop+=readSize;
            }
            else
            {
                if(writeThread->blockArrayStart==0)
                {
                    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] writeThread->blockArrayStart==0, buffer full");
                    readSize=0;
                }
                else
                {
                    readSize=fread(writeThread->blockArray,writeThread->blockArrayStart-1,1,file);
                    if(readSize>=0)
                        writeThread->blockArrayStop=readSize;
                }
            }
        }
        else
        {
            blockArray=file.fread(1024*1024);
        }
        #ifdef ULTRACOPIER_PLUGIN_DEBUG
        stat=Idle;
        #endif

        if(readSize<0)
        {
            errorString_internal=tr("Unable to read the source file: ").toStdString()+", errno: "+std::to_string(errno);
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] "+QStringLiteral("file.error()!=QFile::NoError: %1, error: ").arg(QString::number(file.error())).toStdString()+errorString_internal);
            isInReadLoop=false;
            emit error();
            return;
        }
        readSize=blockArray.size();
        if(readSize>0)
        {
            #ifdef ULTRACOPIER_PLUGIN_DEBUG
            stat=WaitWritePipe;
            #endif
            if(!writeThread->write(blockArray))//speed limitation here
            {
                if(!stopIt)
                {
                    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] stopped because the write is stopped: "+std::to_string(lastGoodPosition));
                    stopIt=true;
                }
            }

            #ifdef ULTRACOPIER_PLUGIN_DEBUG
            stat=Idle;
            #endif

            if(stopIt)
            {
                ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] stopIt == true, then quit");
                isInReadLoop=false;
                internalClose();//need re-open the destination and then the source
                return;
            }
            lastGoodPosition+=blockArray.size();
        }
        /*
        if(lastGoodPosition>16*1024)
        {
            ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,QStringLiteral("[")+QString::number(id)+QStringLiteral("] ")+QStringLiteral("Test error in reading: %1 (%2)").arg(file.errorString()).arg(file.error()));
            errorString_internal=QStringLiteral("Test error in reading: %1 (%2)").arg(file.errorString()).arg(file.error());
            isInReadLoop=false;
            emit error();
            return;
        }
        */
    }
    while(readSize>0 && !stopIt);
    if(lastGoodPosition>file.size())
    {
        errorString_internal=tr("File truncated during the read, possible data change").toStdString();
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] "+QStringLiteral("Source truncated during the read: %1 (%2)").arg(file.errorString()).arg(QString::number(file.error())).toStdString());
        isInReadLoop=false;
        emit error();
        return;
    }
    isInReadLoop=false;
    if(stopIt)
    {
        stopIt=false;
        return;
    }
    emit readIsStopped();//will product by signal connection writeThread->endIsDetected();
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] stop the read");
}

void ReadThread::startRead()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start");
    if(tryStartRead)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] already in try start");
        return;
    }
    if(isInReadLoop)
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] double event dropped");
    else
    {
        tryStartRead=true;
        emit internalStartRead();
    }
}

void ReadThread::internalCloseSlot()
{
    internalClose();
}

void ReadThread::internalClose(bool callByTheDestructor)
{
    /// \note never send signal here, because it's called by the destructor
    //ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,QStringLiteral("[")+QString::number(id)+QStringLiteral("] start"));
    if(!fakeMode)
    {
        if(file!=NULL)
        {
            fclose(file);
            isInReadLoop=false;
        }
    }
    if(!callByTheDestructor)
        emit closed();
}

/** \brief set block size
\param block the new block size in B
\return Return true if succes */
bool ReadThread::setBlockSize(const int blockSize)
{
    //can be smaller than min block size to do correct speed limitation
    if(blockSize>1 && blockSize<ULTRACOPIER_PLUGIN_MAX_BLOCK_SIZE*1024)
    {
        this->blockSize=blockSize;
        //set the new max speed because the timer have changed
        return true;
    }
    else
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"block size out of range: "+std::to_string(blockSize));
        return false;
    }
}

/// \brief do the fake open
void ReadThread::fakeOpen()
{
    fakeMode=true;
    emit opened();
}

/// \brief do the fake writeIsStarted
void ReadThread::fakeReadIsStarted()
{
    emit readIsStarted();
}

/// \brief do the fake writeIsStopped
void ReadThread::fakeReadIsStopped()
{
    emit readIsStopped();
}

int64_t ReadThread::getLastGoodPosition() const
{
    /*if(lastGoodPosition>file.size())
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Critical,QStringLiteral("[")+QString::number(id)+QStringLiteral("] Bug, the lastGoodPosition is greater than the file size!"));
        return file.size();
    }
    else*/
    return lastGoodPosition;
}

//reopen after an error
void ReadThread::reopen()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start");
    if(isInReadLoop)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] try reopen where read is not finish");
        return;
    }
    stopIt=true;
    emit internalStartReopen();
}

bool ReadThread::internalReopen()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start");
    stopIt=false;
    if(file!=NULL)
        fclose(file);
    if(size_at_open!=file.size() && mtime_at_open!=(uint64_t)QFileInfo(file).lastModified().toMSecsSinceEpoch()/1000)
    {
        ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Warning,"["+std::to_string(id)+"] source file have changed since the last open, restart all");
        //fix this function like the close function
        if(internalOpen(true))
        {
            emit resumeAfterErrorByRestartAll();
            return true;
        }
        else
            return false;
    }
    else
    {
        //fix this function like the close function
        if(internalOpen(false))
        {
            emit resumeAfterErrorByRestartAtTheLastPosition();
            return true;
        }
        else
            return false;
    }
}

//set the write thread
void ReadThread::setWriteThread(WriteThread * writeThread)
{
    this->writeThread=writeThread;
}

#ifdef ULTRACOPIER_PLUGIN_DEBUG
//to set the id
void ReadThread::setId(int id)
{
    this->id=id;
}
#endif

void ReadThread::seekToZeroAndWait()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start");
    stopIt=true;
    seekToZero=true;
    emit checkIfIsWait();
}

void ReadThread::isInWait()
{
    ULTRACOPIER_DEBUGCONSOLE(Ultracopier::DebugLevel_Notice,"["+std::to_string(id)+"] start");
    if(seekToZero)
    {
        stopIt=false;
        seekToZero=false;
        if(file!=NULL)
        {
            lastGoodPosition=0;
            seek(0);
        }
        else
            internalOpen(true);
        emit isSeekToZeroAndWait();
    }
}

bool ReadThread::isReading() const
{
    return isInReadLoop;
}
